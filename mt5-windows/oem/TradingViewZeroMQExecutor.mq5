//+------------------------------------------------------------------+
//|                                TradingViewZeroMQExecutor.mq5     |
//|                         TradingView -> MT5 ZeroMQ Trade Executor |
//|                                                                  |
//| v3.1: Added spread filter with retry logic                       |
//| v3.2: Added Resting Limit Order support (ARM_LONG/ARM_SHORT/     |
//|       CANCEL_LONG/CANCEL_SHORT) and bracket MODIFY (Fast-Move    |
//|       TP Extension) - see the big comment block below OnInit()  |
//|       for what's new and why.                                    |
//| v3.3: BUY/SELL (non double-down) skips when a resting-limit      |
//|       position is already open in that direction, and clears     |
//|       same-direction resting limits before a market entry.       |
//| v3.4: Failed closes are queued and retried until they succeed    |
//|       (e.g. after a broker disconnect); broker connection loss / |
//|       recovery is logged and pushed as a notification; retcode   |
//|       10046 (hedge prohibited) gets a readable explanation.      |
//|       Closes run oldest-first (FIFO accounts reject anything else |
//|       with 10045); MODIFY updates every position on the symbol.  |
//| v3.5: Execution reports for the Pinebridge Dashboard: a second   |
//|       ZeroMQ socket PUSHes an ack per signal (echoing signal_id, |
//|       with requested/fill price and retcode), every deal on this |
//|       magic (incl. broker SL/TP closes) and an account heartbeat |
//|       back to the bridge (ReportAddress). Trading never waits on |
//|       it: sends are non-blocking and dropped if the bridge is    |
//|       unreachable.                                               |
//| v3.6: Close acks report the exit fill (volume-weighted price of  |
//|       the closed legs) and the quote at send, so the dashboard   |
//|       shows exit slippage too.                                   |
//| v3.7: Strategy attribution. A signal tagged strategy=<tag> comes |
//|       with magic_slot (1-999, from the bridge); its orders open  |
//|       with magic MagicNumber*1000+slot, so every deal of the     |
//|       position - broker SL/TP closes too - carries the strategy. |
//|       That whole magic family counts as this EA's own. Closes    |
//|       use the position's magic; deal reports and the heartbeat   |
//|       include magic and order comment. Untagged = as before.     |
//|                                                                  |
//| Receives signals INSTANTLY via ZeroMQ PULL socket from Flask.   |
//| Latency: ~1-5ms from Flask to MT5                                |
//|                                                                  |
//| REQUIRES: MQL5-ZeroMQ library (ding9736)                         |
//|   1. Download from: github.com/ding9736/MQL5-ZeroMQ              |
//|   2. Copy ZeroMQ folder to MQL5/Include/                         |
//|   3. Copy libzmq.dll & libsodium.dll to MQL5/Libraries/          |
//|   4. Enable "Allow DLL imports" in MT5 Options                   |
//+------------------------------------------------------------------+
#property copyright "Pinebridge EA (TradingView ZeroMQ Executor) v3.7"
#property link      "https://github.com/ding9736/MQL5-ZeroMQ"
#property version   "3.70"
#property strict

// ZeroMQ library (ding9736 version)
#include <ZeroMQ/ZeroMQ.mqh>

// Trading includes
#include <Trade/Trade.mqh>

//+------------------------------------------------------------------+
//| Input Parameters                                                  |
//+------------------------------------------------------------------+
input string   ZmqBindAddress = "tcp://*:5555";   // ZeroMQ bind address (PULL)
input int      ZmqTimeoutMs   = 10;               // ZMQ receive timeout (ms)
input ulong    MagicNumber    = 777777;           // Magic number for trades
input double   DefaultLotSize = 0.01;             // Fallback lot size
input int      Slippage       = 3;                // Slippage in points
input int      TimerIntervalMs = 10;              // Timer interval for checking messages (ms)
input bool     EnableLogging  = true;             // Verbose logging

//--- Spread Filter ---
input bool     EnableSpreadFilter  = true;        // Enable spread filter
input double   MaxSpreadPips       = 3.0;         // Max allowed spread (pips)
input bool     EnableSpreadRetry   = true;        // Retry if spread too wide
input int      SpreadRetryAttempts = 5;           // Number of retry attempts
input int      SpreadRetryDelayMs  = 2000;        // Delay between retries (ms)

//--- Resting Limit Orders (v3.2) ---
input bool     EnableLimitOrders   = true;        // Honor ARM_LONG/ARM_SHORT/CANCEL_* signals
input int      LimitOrderExpiryMin = 0;           // Pending order expiry in minutes (0 = GTC, no expiry)

//--- Reliability (v3.4) ---
input int      CloseRetrySec       = 10;          // Retry a failed close every N seconds while connected
input int      CloseRetryMaxMin    = 1440;        // Give up retrying a failed close after N minutes (0 = never)
input bool     NotifyConnection    = true;        // Push notification on broker disconnect/reconnect (needs MetaQuotes ID set in Options > Notifications)

//--- Execution reports for the Pinebridge Dashboard (v3.5) ---
input bool     EnableReports       = true;        // Push execution reports + heartbeat to the bridge
input string   ReportAddress       = "tcp://172.17.0.1:5556"; // Bridge report port (Docker host from this server; else the bridge's LAN IP)
input int      HeartbeatSec        = 30;          // Account heartbeat interval (seconds)

//+------------------------------------------------------------------+
//| Global Variables                                                  |
//+------------------------------------------------------------------+
ZmqContext *g_context = NULL;
ZmqSocket  *g_pullSocket = NULL;
CTrade      trade;
bool        g_initialized = false;

int         g_signalsReceived = 0;
int         g_tradesExecuted = 0;
int         g_tradesFailed = 0;
int         g_spreadRejected = 0;
int         g_limitOrdersArmed = 0;
int         g_limitOrdersCancelled = 0;
int         g_bracketsModified = 0;
uint        g_lastLogTime = 0;

// [v3.4] Tickets whose close failed; retried from OnTimer until they close
// or disappear (e.g. hit their broker-side SL/TP in the meantime).
ulong       g_retryTickets[];
datetime    g_retryQueuedAt[];
uint        g_lastRetryTick = 0;
int         g_closesRetried = 0;
bool        g_wasConnected = true;

// [v3.5] Execution reports. g_ack* collect the outcome of the signal being
// processed; ProcessSignal() resets them and SendAck() reports them.
ZmqSocket  *g_reportSocket = NULL;
uint        g_lastHeartbeatTick = 0;
string      g_lastLog = "";
ulong       g_ackOrder = 0;
double      g_ackReqPrice = 0;
double      g_ackFillPrice = 0;
double      g_ackVolume = 0;
uint        g_ackRetcode = 0;
string      g_ackClosed = "";
datetime    g_disconnectedAt = 0;

//+------------------------------------------------------------------+
//| Expert initialization function                                    |
//+------------------------------------------------------------------+
int OnInit()
{
   Log("====================================================");
   Log("Pinebridge EA (TradingView ZeroMQ Executor) v3.7 Starting...");
   Log("  Using: ding9736/MQL5-ZeroMQ library");
   Log("====================================================");

   // Configure trade object
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);
   trade.SetAsyncMode(false);

   // Initialize ZeroMQ
   if(!InitZeroMQ())
   {
      Log("ERROR: ZeroMQ initialization failed!");
      Log("Make sure:");
      Log("  1. DLL imports are allowed (Tools -> Options -> Expert Advisors)");
      Log("  2. libzmq.dll and libsodium.dll are in MQL5/Libraries/");
      Log("  3. ZeroMQ folder is in MQL5/Include/");
      return INIT_FAILED;
   }

   InitReports();

   // Set timer for checking messages (as fast as possible)
   EventSetMillisecondTimer(TimerIntervalMs);

   Log("====================================================");
   Log("OK: ZeroMQ PULL socket bound to: " + ZmqBindAddress);
   Log("  Waiting for signals from Flask...");
   Log("  Timer interval: " + IntegerToString(TimerIntervalMs) + "ms");
   Log("  Magic Number: " + IntegerToString(MagicNumber));

   if(EnableSpreadFilter)
   {
      Log("  Spread Filter: ON (max " + DoubleToString(MaxSpreadPips, 1) + " pips)");
      if(EnableSpreadRetry)
         Log("  Spread Retry: " + IntegerToString(SpreadRetryAttempts) + " attempts, "
             + IntegerToString(SpreadRetryDelayMs) + "ms delay");
      else
         Log("  Spread Retry: OFF (signal discarded immediately)");
   }
   else
      Log("  Spread Filter: OFF");

   Log("  Close retry: every " + IntegerToString(CloseRetrySec) + "s"
       + (CloseRetryMaxMin > 0 ? (", give up after " + IntegerToString(CloseRetryMaxMin) + "min") : ", no time limit")
       + " | Connection notifications: " + (NotifyConnection ? "ON" : "OFF"));
   Log("  Resting Limit Orders: " + (EnableLimitOrders ? "ON" : "OFF")
       + (EnableLimitOrders ? (LimitOrderExpiryMin > 0 ? (" (expiry " + IntegerToString(LimitOrderExpiryMin) + "min)") : " (GTC)") : ""));

   Log("====================================================");

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| v3.2 CHANGELOG - Resting Limit Orders + bracket MODIFY            |
//| ------------------------------------------------------------------|
//| The Pine script (AIO BLOCKS v8, v19.10) added a "Resting Limit    |
//| Order Entries" feature that runs alongside its existing market-   |
//| entry pipeline (BUY/SELL/CLOSELONG/CLOSESHORT/CLOSE below, all    |
//| unchanged). It tracks the nearest eligible-but-untouched zone/OB  |
//| per direction and sends:                                          |
//|   ARM_LONG / ARM_SHORT     - rest a limit order at limit_price    |
//|                              with the given size/sl_pips/tp_pips, |
//|                              tagged with zone_id/zone_src.        |
//|   CANCEL_LONG / CANCEL_SHORT - pull a previously armed resting    |
//|                              order for that zone_id.               |
//| Pine only ever arms ONE resting order per direction at a time,    |
//| and sends a cancel before arming a replacement whenever the       |
//| candidate zone changes. This EA mirrors that invariant:            |
//|   - ARM_* first deletes any existing pending limit order for that |
//|     symbol/magic/direction (regardless of its zone_id - a new arm |
//|     always supersedes whatever was resting), THEN places the new  |
//|     one. The zone_id is stamped into the order comment (e.g.      |
//|     "TV-ZMQ|41821") so a later CANCEL can verify it's cancelling   |
//|     the order it thinks it is.                                     |
//|   - CANCEL_* only deletes the resting order if its stamped         |
//|     zone_id matches the cancel's zone_id. A mismatch means a newer|
//|     ARM already replaced it (a stale/out-of-order cancel arrived   |
//|     late) - that's logged and skipped rather than deleting the     |
//|     wrong order.                                                    |
//|   - ARM_* also refuses to place a resting order if a position is  |
//|     already open for that symbol/magic. Pine's own "flat" gate     |
//|     normally prevents this, but Pine's simulated position and the |
//|     EA's real broker-side position CAN diverge for a limit-order-  |
//|     originated trade (see the Pine changelog, PART G) - this is    |
//|     the EA-side backstop for that gap.                             |
//|   - The EA does NOT apply the spread filter to ARM_* placement:    |
//|     a resting limit order doesn't fill immediately, so an instant- |
//|     execution spread check doesn't apply; the broker's own spread  |
//|     at the moment the order actually triggers governs the fill.    |
//|                                                                     |
//| Separately, "MODIFY" is the pre-existing Fast-Move TP Extension    |
//| alert (Pine already had this - the EA previously had NO handling   |
//| for it at all, so every FTP bracket-move alert reaching this EA    |
//| fell into ProcessSignal's "Unknown action" branch and was silently |
//| dropped). It moves an ALREADY-OPEN position's SL and/or TP to new  |
//| absolute prices - there's no entry price on this alert, so         |
//| sl_price/tp_price are used directly rather than converted from     |
//| pips. Either field may be absent (the script's "Include SL/TP      |
//| Price in Open/Modify Alerts" toggles can turn either off               |
//| independently) - an absent field leaves that side of the bracket   |
//| unchanged rather than being treated as "set to 0".                 |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
//| Initialize ZeroMQ sockets                                         |
//+------------------------------------------------------------------+
bool InitZeroMQ()
{
   // Create ZeroMQ context
   g_context = new ZmqContext();
   if(!g_context.isValid())
   {
      Log("ERROR: Failed to create ZeroMQ context");
      return false;
   }
   Log("OK: ZeroMQ context created");

   // Create PULL socket - Flask will PUSH to this
   g_pullSocket = new ZmqSocket(g_context.ref(), ZMQ_SOCKET_PULL);
   if(!g_pullSocket.isValid())
   {
      Log("ERROR: Failed to create PULL socket");
      return false;
   }
   Log("OK: PULL socket created");

   // Set receive timeout (non-blocking with short timeout)
   g_pullSocket.setReceiveTimeout(ZmqTimeoutMs);

   // Set high water mark (buffer size)
   g_pullSocket.setReceiveHighWaterMark(100);

   // Bind to address (Flask will connect to this)
   if(!g_pullSocket.bind(ZmqBindAddress))
   {
      Log("ERROR: Failed to bind PULL socket to " + ZmqBindAddress);
      Log("  Check if port is already in use");
      return false;
   }

   Log("OK: PULL socket bound to " + ZmqBindAddress);
   g_initialized = true;
   return true;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                  |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();

   Log("====================================================");
   Log("Shutting down ZeroMQ Executor...");
   Log("  Signals received: " + IntegerToString(g_signalsReceived));
   Log("  Trades executed: " + IntegerToString(g_tradesExecuted));
   Log("  Trades failed: " + IntegerToString(g_tradesFailed));
   Log("  Spread rejected: " + IntegerToString(g_spreadRejected));
   Log("  Limit orders armed: " + IntegerToString(g_limitOrdersArmed));
   Log("  Limit orders cancelled: " + IntegerToString(g_limitOrdersCancelled));
   Log("  Brackets modified: " + IntegerToString(g_bracketsModified));
   Log("  Closes recovered by retry: " + IntegerToString(g_closesRetried));
   if(ArraySize(g_retryTickets) > 0)
      Log("WARNING: " + IntegerToString(ArraySize(g_retryTickets)) + " failed close(s) still pending - check open positions manually");

   // Clean up ZeroMQ
   if(g_reportSocket != NULL)
   {
      g_reportSocket.disconnect(ReportAddress);
      delete g_reportSocket;
      g_reportSocket = NULL;
   }

   if(g_pullSocket != NULL)
   {
      g_pullSocket.unbind(ZmqBindAddress);
      delete g_pullSocket;
      g_pullSocket = NULL;
   }

   if(g_context != NULL)
   {
      g_context.shutdown();
      delete g_context;
      g_context = NULL;
   }

   g_initialized = false;
   Log("OK: Shutdown complete");
   Log("====================================================");
}

//+------------------------------------------------------------------+
//| Timer function - check for ZeroMQ messages                        |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_initialized) return;

   // Check for messages
   CheckForMessages();

   MonitorConnection();
   RetryFailedCloses();
   SendHeartbeat(false);
}

//+------------------------------------------------------------------+
//| [v3.4] Log + notify when the terminal loses / regains the broker. |
//| While disconnected every trade request fails (10031), so this is  |
//| the thing to know about in real time, not after the fact.         |
//+------------------------------------------------------------------+
void MonitorConnection()
{
   bool connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   if(connected == g_wasConnected) return;
   g_wasConnected = connected;

   string msg;
   if(!connected)
   {
      g_disconnectedAt = TimeLocal();   // server time stops advancing while disconnected
      msg = "Pinebridge: MT5 lost connection to the broker - trade signals will fail until it reconnects";
   }
   else
   {
      int secs = g_disconnectedAt > 0 ? (int)(TimeLocal() - g_disconnectedAt) : 0;
      string took = secs < 120 ? (IntegerToString(secs) + "s") : ("~" + IntegerToString(secs / 60) + " min");
      msg = "Pinebridge: MT5 reconnected to the broker after " + took
            + (ArraySize(g_retryTickets) > 0 ? ("; retrying " + IntegerToString(ArraySize(g_retryTickets)) + " failed close(s)") : "");
   }
   Print("[ZMQ] " + (connected ? "OK: " : "WARNING: ") + msg);   // always logged, regardless of EnableLogging
   if(NotifyConnection)
      SendNotification(StringSubstr(msg, 0, 255));
   SendHeartbeat(true);   // [v3.5] tell the dashboard straight away
}

//+------------------------------------------------------------------+
//| [v3.4] Queue a ticket whose close failed, so it's retried.        |
//+------------------------------------------------------------------+
void QueueCloseRetry(ulong ticket)
{
   int n = ArraySize(g_retryTickets);
   for(int i = 0; i < n; i++)
      if(g_retryTickets[i] == ticket) return;
   ArrayResize(g_retryTickets, n + 1);
   ArrayResize(g_retryQueuedAt, n + 1);
   g_retryTickets[n] = ticket;
   g_retryQueuedAt[n] = TimeLocal();
   Print("[ZMQ] WARNING: close of ticket " + IntegerToString((long)ticket) + " failed - queued for retry every "
         + IntegerToString(CloseRetrySec) + "s");
}

void RemoveCloseRetry(int index)
{
   int last = ArraySize(g_retryTickets) - 1;
   g_retryTickets[index] = g_retryTickets[last];
   g_retryQueuedAt[index] = g_retryQueuedAt[last];
   ArrayResize(g_retryTickets, last);
   ArrayResize(g_retryQueuedAt, last);
}

//+------------------------------------------------------------------+
//| [v3.4] Retry queued closes. Pine already exited these positions,  |
//| so closing them late is still correct; only the exact tickets     |
//| that failed are retried, never a position opened afterwards.      |
//+------------------------------------------------------------------+
void RetryFailedCloses()
{
   if(ArraySize(g_retryTickets) == 0) return;
   if(GetTickCount() - g_lastRetryTick < (uint)MathMax(CloseRetrySec, 1) * 1000) return;
   g_lastRetryTick = GetTickCount();
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) return;

   // Drop tickets that are already gone (closed by SL/TP or manually).
   for(int i = ArraySize(g_retryTickets) - 1; i >= 0; i--)
   {
      if(PositionSelectByTicket(g_retryTickets[i])) continue;
      Log("Retry: ticket " + IntegerToString((long)g_retryTickets[i]) + " is no longer open (closed by SL/TP or manually) - dropping it");
      RemoveCloseRetry(i);
   }

   // Retry the rest oldest-first, same FIFO rule as ClosePositions().
   ulong openTickets[];
   int n = CollectPositionsFifo("", -1, openTickets);
   for(int k = 0; k < n; k++)
   {
      int i = -1;
      for(int j = 0; j < ArraySize(g_retryTickets); j++)
         if(g_retryTickets[j] == openTickets[k]) { i = j; break; }
      if(i < 0) continue;

      ulong ticket = g_retryTickets[i];
      if(ClosePositionAsOwner(ticket))
      {
         Print("[ZMQ] OK: Closed ticket " + IntegerToString((long)ticket) + " on retry");
         g_closesRetried++;
         RemoveCloseRetry(i);
         continue;
      }
      if(CloseRetryMaxMin > 0 && TimeLocal() - g_retryQueuedAt[i] > CloseRetryMaxMin * 60)
      {
         string msg = "Pinebridge: gave up closing ticket " + IntegerToString((long)ticket) + " after "
                      + IntegerToString(CloseRetryMaxMin) + " min (" + RetcodeText() + ") - close it manually";
         Print("[ZMQ] ERROR: " + msg);
         if(NotifyConnection) SendNotification(StringSubstr(msg, 0, 255));   // 255-char push limit
         RemoveCloseRetry(i);
      }
   }
}

//+------------------------------------------------------------------+
//| [v3.5] Execution reports -> bridge (Pinebridge Dashboard)         |
//| A PUSH socket that CONNECTS to the bridge (outbound from the VM,  |
//| so no Windows firewall rule is needed). Sends are non-blocking;   |
//| if the bridge is down, up to 1000 reports queue and the rest are  |
//| dropped - trading never waits on reporting.                       |
//+------------------------------------------------------------------+
void InitReports()
{
   if(!EnableReports || ReportAddress == "") { Log("  Execution reports: OFF"); return; }
   g_reportSocket = new ZmqSocket(g_context.ref(), ZMQ_SOCKET_PUSH);
   if(!g_reportSocket.isValid())
   {
      Log("WARNING: Could not create the report socket - dashboard reports disabled");
      delete g_reportSocket;
      g_reportSocket = NULL;
      return;
   }
   g_reportSocket.setLinger(0);
   g_reportSocket.setSendHighWaterMark(1000);
   if(!g_reportSocket.connect(ReportAddress))
   {
      Log("WARNING: Could not connect reports to " + ReportAddress + " - dashboard reports disabled");
      delete g_reportSocket;
      g_reportSocket = NULL;
      return;
   }
   Log("  Execution reports: -> " + ReportAddress);
   SendHeartbeat(true);
}

void SendReport(string json)
{
   if(g_reportSocket == NULL) return;
   g_reportSocket.send(json, ZMQ_FLAG_DONTWAIT);
}

string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   StringReplace(s, "\t", " ");
   return s;
}

string JStr(string key, string value) { return "\"" + key + "\":\"" + JsonEscape(value) + "\""; }
string JNum(string key, double value, int digits) { return "\"" + key + "\":" + DoubleToString(value, digits); }
string JInt(string key, long value) { return "\"" + key + "\":" + IntegerToString(value); }
string JBool(string key, bool value) { return "\"" + key + "\":" + (value ? "true" : "false"); }

//+------------------------------------------------------------------+
//| [v3.7] Strategy magic numbers. Untagged orders use MagicNumber;  |
//| a tagged entry uses MagicNumber * 1000 + slot (slot 1-999).      |
//+------------------------------------------------------------------+
bool IsOurMagic(long magic)
{
   if(magic <= 0) return false;
   if((ulong)magic == MagicNumber) return true;
   return (ulong)magic / 1000 == MagicNumber && magic % 1000 != 0;
}

ulong StrategyMagic(long slot)
{
   // Leave headroom so MagicNumber * 1000 can't overflow.
   if(slot < 1 || slot > 999 || MagicNumber > 9000000000000000) return MagicNumber;
   return MagicNumber * 1000 + (ulong)slot;
}

// Close with the position's own magic, so the closing deal carries
// its strategy as well (broker SL/TP closes already do).
bool ClosePositionAsOwner(ulong ticket)
{
   if(PositionSelectByTicket(ticket))
      trade.SetExpertMagicNumber((ulong)PositionGetInteger(POSITION_MAGIC));
   bool ok = trade.PositionClose(ticket);
   trade.SetExpertMagicNumber(MagicNumber);
   return ok;
}

// Called right after a successful market BUY/SELL.
void RecordFill(double requestedPrice)
{
   g_ackOrder = trade.ResultOrder();
   g_ackReqPrice = requestedPrice;
   g_ackFillPrice = trade.ResultPrice();
   g_ackVolume = trade.ResultVolume();
   g_ackRetcode = trade.ResultRetcode();
   // Some execution modes leave result.price at 0: read the deal instead.
   if(g_ackFillPrice <= 0 && trade.ResultDeal() > 0 && HistoryDealSelect(trade.ResultDeal()))
      g_ackFillPrice = HistoryDealGetDouble(trade.ResultDeal(), DEAL_PRICE);
}

void SendAck(string json, bool ok, uint elapsedMs)
{
   if(g_reportSocket == NULL) return;
   string signalId = ParseJsonString(json, "signal_id");
   if(signalId == "") return;   // not from a dashboard-aware bridge
   string symbol = ParseJsonString(json, "symbol");
   int digits = symbol != "" ? (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS) : 5;
   string closed = g_ackClosed == "" ? "[]" : "[" + g_ackClosed + "]";
   SendReport("{" + JStr("type", "ack") + "," + JStr("signal_id", signalId) + "," + JBool("ok", ok)
              + "," + JInt("retcode", g_ackRetcode) + "," + JStr("detail", g_lastLog)
              + "," + JStr("symbol", symbol) + "," + JInt("order", (long)g_ackOrder)
              + "," + JNum("requested_price", g_ackReqPrice, digits) + "," + JNum("fill_price", g_ackFillPrice, digits)
              + "," + JNum("volume", g_ackVolume, 2)
              + "," + JNum("tick_size", symbol != "" ? SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE) : 0.0, 10)
              + "," + JNum("tick_value", symbol != "" ? SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE) : 0.0, 6)
              + "," + JInt("ea_ms", elapsedMs) + ",\"closed_tickets\":" + closed + "}");
}

string DealReasonText(long reason)
{
   switch((int)reason)
   {
      case DEAL_REASON_SL:     return "sl";
      case DEAL_REASON_TP:     return "tp";
      case DEAL_REASON_SO:     return "stop_out";
      case DEAL_REASON_EXPERT: return "ea";
      case DEAL_REASON_CLIENT: case DEAL_REASON_MOBILE: case DEAL_REASON_WEB: return "manual";
   }
   return "other";
}

// Every deal on this EA's magic number, including SL/TP closes by the broker.
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(g_reportSocket == NULL || trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   ulong deal = trans.deal;
   if(!HistoryDealSelect(deal)) return;
   if(!IsOurMagic(HistoryDealGetInteger(deal, DEAL_MAGIC))) return;
   long type = HistoryDealGetInteger(deal, DEAL_TYPE);
   if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) return;

   long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
   string entryText = entry == DEAL_ENTRY_IN ? "in" : entry == DEAL_ENTRY_OUT ? "out"
                    : entry == DEAL_ENTRY_INOUT ? "inout" : "out_by";
   string symbol = HistoryDealGetString(deal, DEAL_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   SendReport("{" + JStr("type", "deal") + "," + JInt("deal", (long)deal)
              + "," + JInt("order", HistoryDealGetInteger(deal, DEAL_ORDER))
              + "," + JInt("position_id", HistoryDealGetInteger(deal, DEAL_POSITION_ID))
              + "," + JStr("symbol", symbol) + "," + JStr("side", type == DEAL_TYPE_BUY ? "buy" : "sell")
              + "," + JStr("entry", entryText)
              + "," + JNum("volume", HistoryDealGetDouble(deal, DEAL_VOLUME), 2)
              + "," + JNum("price", HistoryDealGetDouble(deal, DEAL_PRICE), digits)
              + "," + JNum("profit", HistoryDealGetDouble(deal, DEAL_PROFIT), 2)
              + "," + JNum("commission", HistoryDealGetDouble(deal, DEAL_COMMISSION), 2)
              + "," + JNum("swap", HistoryDealGetDouble(deal, DEAL_SWAP), 2)
              + "," + JNum("time", HistoryDealGetInteger(deal, DEAL_TIME_MSC) / 1000.0, 3)
              + "," + JStr("reason", DealReasonText(HistoryDealGetInteger(deal, DEAL_REASON)))
              + "," + JInt("magic", HistoryDealGetInteger(deal, DEAL_MAGIC))
              + "," + JStr("comment", HistoryDealGetString(deal, DEAL_COMMENT)) + "}");
}

void SendHeartbeat(bool force)
{
   if(g_reportSocket == NULL) return;
   if(!force && GetTickCount() - g_lastHeartbeatTick < (uint)MathMax(HeartbeatSec, 5) * 1000) return;
   g_lastHeartbeatTick = GetTickCount();

   string positions = "";
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !IsOurMagic(PositionGetInteger(POSITION_MAGIC))) continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      positions += (positions == "" ? "" : ",") + "{" + JInt("ticket", (long)ticket) + "," + JStr("symbol", symbol)
                   + "," + JStr("side", PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell")
                   + "," + JNum("volume", PositionGetDouble(POSITION_VOLUME), 2)
                   + "," + JNum("open_price", PositionGetDouble(POSITION_PRICE_OPEN), digits)
                   + "," + JNum("current_price", PositionGetDouble(POSITION_PRICE_CURRENT), digits)
                   + "," + JNum("sl", PositionGetDouble(POSITION_SL), digits)
                   + "," + JNum("tp", PositionGetDouble(POSITION_TP), digits)
                   + "," + JNum("profit", PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP), 2)
                   + "," + JNum("opened_at", (double)PositionGetInteger(POSITION_TIME), 0)
                   + "," + JInt("magic", PositionGetInteger(POSITION_MAGIC))
                   + "," + JStr("comment", PositionGetString(POSITION_COMMENT)) + "}";
   }

   SendReport("{" + JStr("type", "account")
              + "," + JNum("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2)
              + "," + JNum("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2)
              + "," + JNum("margin", AccountInfoDouble(ACCOUNT_MARGIN), 2)
              + "," + JNum("free_margin", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2)
              + "," + JStr("currency", AccountInfoString(ACCOUNT_CURRENCY))
              + "," + JStr("server", AccountInfoString(ACCOUNT_SERVER))
              + "," + JBool("connected", (bool)TerminalInfoInteger(TERMINAL_CONNECTED))
              + "," + JBool("terminal_trade_allowed", (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
              + "," + JBool("ea_trade_allowed", (bool)MQLInfoInteger(MQL_TRADE_ALLOWED))
              + "," + JStr("ea_version", "3.7")
              + "," + JInt("magic", (long)MagicNumber)
              + "," + JInt("pending_close_retries", ArraySize(g_retryTickets))
              + ",\"positions\":[" + positions + "]}");
}

//+------------------------------------------------------------------+
//| [v3.4] "<code> - <description>" for the last trade result, with   |
//| a readable explanation for codes CTrade doesn't describe.         |
//+------------------------------------------------------------------+
string RetcodeText()
{
   uint code = trade.ResultRetcode();
   string desc = trade.ResultRetcodeDescription();
   if(code == 10045)
      desc = "FIFO close rule: an older position on this symbol must be closed first (US/NFA FIFO rules)";
   else if(code == 10046)
      desc = "hedge prohibited: an opposite position is open on this symbol and the account does not allow hedging (e.g. US/NFA FIFO rules)";
   return IntegerToString(code) + " - " + desc;
}

//+------------------------------------------------------------------+
//| Check for incoming ZeroMQ messages                                |
//+------------------------------------------------------------------+
void CheckForMessages()
{
   if(g_pullSocket == NULL) return;

   // Try to receive a message (with timeout set, this won't block long)
   string message;

   if(g_pullSocket.recv(message))
   {
      uint startTime = GetTickCount();

      if(StringLen(message) > 0)
      {
         g_signalsReceived++;

         Log("----------------------------------------------------");
         Log("SIGNAL RECEIVED via ZeroMQ!");
         Log("Raw: " + message);

         // Process the signal
         bool success = ProcessSignal(message);

         uint elapsed = GetTickCount() - startTime;
         SendAck(message, success, elapsed);
         Log("Processing time: " + IntegerToString(elapsed) + "ms");
         Log("----------------------------------------------------");
      }
   }
   // No message received - this is normal, just return silently
}

//+------------------------------------------------------------------+
//| Get pip value for a symbol                                        |
//+------------------------------------------------------------------+
double GetPipValue(string symbol)
{
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   if(digits == 2 || digits == 3)
      return 0.01;        // JPY pairs
   else if(digits == 4 || digits == 5)
      return 0.0001;      // Standard forex
   else if(digits == 1)
      return 0.1;         // Some indices
   else
      return SymbolInfoDouble(symbol, SYMBOL_POINT) * 10;
}

//+------------------------------------------------------------------+
//| Calculate price from pips distance                                |
//+------------------------------------------------------------------+
double PipsToPrice(string symbol, double pips)
{
   return pips * GetPipValue(symbol);
}

//+------------------------------------------------------------------+
//| Get current spread in pips for a symbol                           |
//+------------------------------------------------------------------+
double GetSpreadPips(string symbol)
{
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return -1.0;  // Error

   double pipValue = GetPipValue(symbol);
   if(pipValue <= 0) return -1.0;

   double spreadPrice = tick.ask - tick.bid;
   return spreadPrice / pipValue;
}

//+------------------------------------------------------------------+
//| Check if spread is acceptable (with optional retry)               |
//| Returns true if trade should proceed, false to reject             |
//+------------------------------------------------------------------+
bool CheckSpread(string symbol)
{
   if(!EnableSpreadFilter)
      return true;  // Filter disabled, always proceed

   double spreadPips = GetSpreadPips(symbol);

   if(spreadPips < 0)
   {
      Log("WARNING: Could not read spread for " + symbol + ", proceeding with trade");
      return true;  // Can't read spread, don't block the trade
   }

   // Check if spread is acceptable
   if(spreadPips <= MaxSpreadPips)
   {
      Log("OK: Spread OK: " + DoubleToString(spreadPips, 1) + " pips (max: "
          + DoubleToString(MaxSpreadPips, 1) + ")");
      return true;
   }

   // Spread too wide - retry or reject
   Log("WARN: Spread too wide: " + DoubleToString(spreadPips, 1) + " pips (max: "
       + DoubleToString(MaxSpreadPips, 1) + ")");

   if(!EnableSpreadRetry)
   {
      Log("REJECT: SPREAD REJECT: No retry enabled, signal discarded");
      g_spreadRejected++;
      return false;
   }

   // Retry loop - spread spikes during news often normalize in seconds
   for(int attempt = 1; attempt <= SpreadRetryAttempts; attempt++)
   {
      Log("  Retry " + IntegerToString(attempt) + "/" + IntegerToString(SpreadRetryAttempts)
          + ": waiting " + IntegerToString(SpreadRetryDelayMs) + "ms...");

      Sleep(SpreadRetryDelayMs);

      spreadPips = GetSpreadPips(symbol);

      if(spreadPips < 0)
      {
         Log("  WARNING: Could not read spread on retry, proceeding");
         return true;
      }

      Log("  Spread now: " + DoubleToString(spreadPips, 1) + " pips");

      if(spreadPips <= MaxSpreadPips)
      {
         Log("  OK: Spread normalized after " + IntegerToString(attempt) + " retries");
         return true;
      }
   }

   // All retries exhausted
   Log("REJECT: SPREAD REJECT: Still " + DoubleToString(spreadPips, 1)
       + " pips after " + IntegerToString(SpreadRetryAttempts) + " retries");
   g_spreadRejected++;
   return false;
}

//+------------------------------------------------------------------+
//| Process trading signal from JSON                                  |
//+------------------------------------------------------------------+
bool ProcessSignal(string json)
{
   Log("=== Processing Signal ===");
   g_ackOrder = 0; g_ackReqPrice = 0; g_ackFillPrice = 0; g_ackVolume = 0;
   g_ackRetcode = 0; g_ackClosed = "";

   // Parse JSON fields
   string action = ParseJsonString(json, "action");
   string symbol = ParseJsonString(json, "symbol");
   double size = ParseJsonDouble(json, "size");
   double slPips = ParseJsonDouble(json, "sl_pips");
   double tpPips = ParseJsonDouble(json, "tp_pips");
   string comment = ParseJsonString(json, "comment");
   string exitReason = ParseJsonString(json, "exit_reason");

   // Use defaults if needed
   if(size <= 0) size = DefaultLotSize;
   if(comment == "") comment = "TV-ZMQ";

   // Convert action to uppercase
   StringToUpper(action);

   Log("Action: " + action);
   Log("Symbol: " + symbol);

   // Execute based on action
   bool result = false;

   if(action == "BUY" || action == "SELL")
   {
      bool isLong = (action == "BUY");
      bool isDd = (ParseJsonDouble(json, "dd") > 0);
      Log("Size: " + DoubleToString(size, 2) + (isDd ? " (double-down add)" : ""));
      Log("SL Pips: " + DoubleToString(slPips, 1));
      Log("TP Pips: " + DoubleToString(tpPips, 1));
      if(!isDd && LimitFilledPositionOpen(symbol, isLong))
      {
         // Pine's bar-close entry for a zone whose resting limit already
         // filled broker-side - executing it would double the position.
         Log("SKIP: " + action + " - a resting-limit " + (isLong ? "long" : "short") + " position is already open for "
             + symbol + " (Pine's bar-close entry for the same move)");
         return false;
      }
      if(!isDd)
         DeletePendingLimitOrders(symbol, isLong ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT, -1,
                                  "superseded by market " + action);
      trade.SetExpertMagicNumber(StrategyMagic((long)ParseJsonDouble(json, "magic_slot")));
      result = ExecuteTrade(action, symbol, size, slPips, tpPips, comment);
      trade.SetExpertMagicNumber(MagicNumber);
   }
   else if(action == "CLOSELONG")
   {
      result = ClosePositions(symbol, POSITION_TYPE_BUY);
   }
   else if(action == "CLOSESHORT")
   {
      result = ClosePositions(symbol, POSITION_TYPE_SELL);
   }
   else if(action == "CLOSE")
   {
      result = ClosePositions(symbol, -1);
   }
   else if(action == "ARM_LONG" || action == "ARM_SHORT")
   {
      if(!EnableLimitOrders)
      {
         Log("SKIP: Resting Limit Orders disabled (EnableLimitOrders=false)");
         return false;
      }
      bool isLong = (action == "ARM_LONG");
      double limitPrice = ParseJsonDouble(json, "limit_price");
      long   zoneId      = (long)StringToInteger(ParseJsonString(json, "zone_id"));
      string zoneSrc      = ParseJsonString(json, "zone_src");

      Log("Limit Price: " + DoubleToString(limitPrice, 5));
      Log("Size: " + DoubleToString(size, 2));
      Log("SL Pips: " + DoubleToString(slPips, 1) + "  TP Pips: " + DoubleToString(tpPips, 1));
      Log("Zone: " + zoneSrc + " #" + IntegerToString((int)zoneId));

      trade.SetExpertMagicNumber(StrategyMagic((long)ParseJsonDouble(json, "magic_slot")));
      result = ArmLimitOrder(symbol, isLong, limitPrice, size, slPips, tpPips, comment, zoneId, zoneSrc);
      trade.SetExpertMagicNumber(MagicNumber);
   }
   else if(action == "CANCEL_LONG" || action == "CANCEL_SHORT")
   {
      if(!EnableLimitOrders)
      {
         Log("SKIP: Resting Limit Orders disabled (EnableLimitOrders=false)");
         return false;
      }
      bool isLong = (action == "CANCEL_LONG");
      long   zoneId  = (long)StringToInteger(ParseJsonString(json, "zone_id"));
      string zoneSrc = ParseJsonString(json, "zone_src");

      Log("Zone: " + zoneSrc + " #" + IntegerToString((int)zoneId));

      result = CancelLimitOrder(symbol, isLong, zoneId);
   }
   else if(action == "MODIFY")
   {
      bool hasSl = JsonHasNonNull(json, "sl_price");
      bool hasTp = JsonHasNonNull(json, "tp_price");
      double slPrice = hasSl ? ParseJsonDouble(json, "sl_price") : 0.0;
      double tpPrice = hasTp ? ParseJsonDouble(json, "tp_price") : 0.0;

      Log("Modify - SL: " + (hasSl ? DoubleToString(slPrice, 5) : "(unchanged)")
          + "  TP: " + (hasTp ? DoubleToString(tpPrice, 5) : "(unchanged)"));

      result = ModifyOpenPosition(symbol, hasSl, slPrice, hasTp, tpPrice);
   }
   else
   {
      Log("ERROR: Unknown action '" + action + "'");
      g_tradesFailed++;
      return false;
   }

   if(action == "BUY" || action == "SELL" || action == "CLOSELONG" || action == "CLOSESHORT" || action == "CLOSE")
   {
      if(result) g_tradesExecuted++;
      else g_tradesFailed++;
   }

   return result;
}

//+------------------------------------------------------------------+
//| Execute a BUY or SELL trade                                       |
//+------------------------------------------------------------------+
bool ExecuteTrade(string action, string symbol, double lots, double slPips, double tpPips, string comment)
{
   if(symbol == "")
   {
      Log("ERROR: No symbol specified");
      return false;
   }

   // Select symbol in Market Watch
   if(!SymbolSelect(symbol, true))
   {
      Log("WARNING: Could not select symbol " + symbol);
   }

   //--- SPREAD CHECK (before anything else) ---
   if(!CheckSpread(symbol))
   {
      Log("REJECT: Trade blocked by spread filter for " + symbol);
      return false;
   }

   // Refresh rates to get latest prices
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      Log("ERROR: Could not get tick for " + symbol);
      return false;
   }

   // Get symbol info
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double ask = tick.ask;
   double bid = tick.bid;

   if(ask == 0 || bid == 0)
   {
      Log("ERROR: Invalid prices for " + symbol + " Ask:" + DoubleToString(ask, digits) + " Bid:" + DoubleToString(bid, digits));
      return false;
   }

   // Log spread at execution time
   double execSpread = (ask - bid) / GetPipValue(symbol);
   Log("Live Prices - Ask: " + DoubleToString(ask, digits) + " Bid: " + DoubleToString(bid, digits)
       + " Spread: " + DoubleToString(execSpread, 1) + " pips");

   // Normalize lot size
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

   if(minLot > 0)
   {
      lots = MathMax(lots, minLot);
      lots = MathMin(lots, maxLot);
      lots = NormalizeDouble(MathFloor(lots / lotStep) * lotStep, 2);
   }

   // Calculate SL and TP prices from pips
   double sl = 0;
   double tp = 0;
   double entryPrice = 0;

   if(action == "BUY")
   {
      entryPrice = ask;
      if(slPips > 0) sl = NormalizeDouble(entryPrice - PipsToPrice(symbol, slPips), digits);
      if(tpPips > 0) tp = NormalizeDouble(entryPrice + PipsToPrice(symbol, tpPips), digits);

      Log("BUY @ " + DoubleToString(entryPrice, digits) +
          " | SL: " + DoubleToString(sl, digits) + " (" + DoubleToString(slPips, 1) + " pips)" +
          " | TP: " + DoubleToString(tp, digits) + " (" + DoubleToString(tpPips, 1) + " pips)");

      if(trade.Buy(lots, symbol, entryPrice, sl, tp, comment))
      {
         Log("OK: BUY EXECUTED! Ticket: " + IntegerToString((int)trade.ResultOrder()) +
             " @ " + DoubleToString(trade.ResultPrice(), digits));
         RecordFill(entryPrice);
         return true;
      }
   }
   else // SELL
   {
      entryPrice = bid;
      if(slPips > 0) sl = NormalizeDouble(entryPrice + PipsToPrice(symbol, slPips), digits);
      if(tpPips > 0) tp = NormalizeDouble(entryPrice - PipsToPrice(symbol, tpPips), digits);

      Log("SELL @ " + DoubleToString(entryPrice, digits) +
          " | SL: " + DoubleToString(sl, digits) + " (" + DoubleToString(slPips, 1) + " pips)" +
          " | TP: " + DoubleToString(tp, digits) + " (" + DoubleToString(tpPips, 1) + " pips)");

      if(trade.Sell(lots, symbol, entryPrice, sl, tp, comment))
      {
         Log("OK: SELL EXECUTED! Ticket: " + IntegerToString((int)trade.ResultOrder()) +
             " @ " + DoubleToString(trade.ResultPrice(), digits));
         RecordFill(entryPrice);
         return true;
      }
   }

   g_ackRetcode = trade.ResultRetcode();
   Log("FAIL: TRADE FAILED: " + RetcodeText());
   return false;
}

//+------------------------------------------------------------------+
//| [v3.4] Tickets of this EA's positions matching symbol ("" = any)  |
//| and type (-1 = any), oldest first (by open time, then ticket).    |
//+------------------------------------------------------------------+
int CollectPositionsFifo(string symbol, int positionType, ulong &tickets[])
{
   long times[];
   int n = 0;
   ArrayResize(tickets, 0);

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC))) continue;
      if(symbol != "" && PositionGetString(POSITION_SYMBOL) != symbol) continue;
      if(positionType >= 0 && PositionGetInteger(POSITION_TYPE) != positionType) continue;

      ArrayResize(tickets, n + 1);
      ArrayResize(times, n + 1);
      tickets[n] = ticket;
      times[n] = PositionGetInteger(POSITION_TIME_MSC);
      n++;
   }

   // Insertion sort - there are only ever a handful of positions.
   for(int a = 1; a < n; a++)
   {
      ulong t = tickets[a];
      long tm = times[a];
      int b = a - 1;
      while(b >= 0 && (times[b] > tm || (times[b] == tm && tickets[b] > t)))
      {
         tickets[b + 1] = tickets[b];
         times[b + 1] = times[b];
         b--;
      }
      tickets[b + 1] = t;
      times[b + 1] = tm;
   }
   return n;
}

//+------------------------------------------------------------------+
//| Close positions for a symbol                                      |
//+------------------------------------------------------------------+
bool ClosePositions(string symbol, int positionType)
{
   string typeStr = positionType == POSITION_TYPE_BUY ? "LONG" :
                    positionType == POSITION_TYPE_SELL ? "SHORT" : "ALL";

   Log("Closing " + typeStr + " positions" + (symbol != "" ? " for " + symbol : ""));

   int closedCount = 0;
   int failedCount = 0;

   // Oldest first: NFA/FIFO accounts reject closing a newer position on a
   // symbol while an older one is open (10045), e.g. the double-down leg.
   ulong tickets[];
   int n = CollectPositionsFifo(symbol, positionType, tickets);
   double quoteSum = 0, fillSum = 0, volSum = 0;   // [v3.6] volume-weighted, for the ack

   for(int i = 0; i < n; i++)
   {
      ulong ticket = tickets[i];
      // Quote this leg will close against: bid for a long, ask for a short.
      double quote = 0, vol = 0;
      if(PositionSelectByTicket(ticket))
      {
         string posSym = PositionGetString(POSITION_SYMBOL);
         bool isLong = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
         quote = SymbolInfoDouble(posSym, isLong ? SYMBOL_BID : SYMBOL_ASK);
         vol = PositionGetDouble(POSITION_VOLUME);
      }
      if(ClosePositionAsOwner(ticket))
      {
         Log("OK: Closed ticket " + IntegerToString((int)ticket));
         closedCount++;
         g_ackClosed += (g_ackClosed == "" ? "" : ",") + IntegerToString((long)ticket);
         double fill = trade.ResultPrice();
         if(fill <= 0 && trade.ResultDeal() > 0 && HistoryDealSelect(trade.ResultDeal()))
            fill = HistoryDealGetDouble(trade.ResultDeal(), DEAL_PRICE);
         if(fill > 0 && vol > 0)
         {
            fillSum += fill * vol;
            quoteSum += quote * vol;
            volSum += vol;
         }
         g_ackRetcode = trade.ResultRetcode();
      }
      else
      {
         g_ackRetcode = trade.ResultRetcode();
         Log("FAIL: Failed to close ticket " + IntegerToString((int)ticket) + ": " + RetcodeText());
         failedCount++;
         QueueCloseRetry(ticket);
      }
   }

   if(volSum > 0)
   {
      g_ackFillPrice = fillSum / volSum;
      g_ackReqPrice = quoteSum / volSum;
      g_ackVolume = volSum;
   }

   Log("Close summary - Closed: " + IntegerToString(closedCount) + ", Failed: " + IntegerToString(failedCount));
   return (failedCount == 0);
}

//+------------------------------------------------------------------+
//| [v3.2] Arm a resting limit order (BUY_LIMIT/SELL_LIMIT)           |
//|                                                                    |
//| Pine only ever arms one resting order per direction at a time, so |
//| this first clears out any existing pending order for the same     |
//| symbol/magic/direction (a new arm always supersedes whatever was  |
//| resting - handles a missed CANCEL gracefully), then places the    |
//| new one with zone_id stamped into the order comment so a later    |
//| CANCEL can verify it's cancelling the right order.                |
//|                                                                    |
//| Deliberately skips the spread filter (CheckSpread): a pending      |
//| order doesn't fill now, so an instant-execution spread check       |
//| doesn't apply here - the broker's spread at the moment the order   |
//| actually triggers is what governs the eventual fill.               |
//+------------------------------------------------------------------+
bool ArmLimitOrder(string symbol, bool isLong, double limitPrice, double lots,
                    double slPips, double tpPips, string baseComment,
                    long zoneId, string zoneSrc)
{
   if(symbol == "" || limitPrice <= 0)
   {
      Log("ERROR: ARM signal missing symbol or limit_price");
      return false;
   }

   if(!SymbolSelect(symbol, true))
      Log("WARNING: Could not select symbol " + symbol);

   // [Backstop] Pine's own simulated 'flat' gate normally prevents arming
   // while a position is open, but Pine's simulated position and this EA's
   // real broker-side position can diverge for a limit-order-originated
   // trade (see the Pine script's v19.10 changelog, PART G). Refuse to
   // rest a new order on top of an already-open position for this symbol.
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong posTicket = PositionGetTicket(i);
      if(posTicket == 0) continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC))) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;

      Log("SKIP: Position already open for " + symbol + " (ticket " + IntegerToString((int)posTicket)
          + ") - refusing to arm a resting limit order on top of it");
      return false;
   }

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   ENUM_ORDER_TYPE orderType = isLong ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;

   // Clear any existing pending order for this symbol/magic/direction first.
   DeletePendingLimitOrders(symbol, orderType, -1, "superseded by new ARM");

   // Normalize lot size (same convention as ExecuteTrade)
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(minLot > 0)
   {
      lots = MathMax(lots, minLot);
      lots = MathMin(lots, maxLot);
      lots = NormalizeDouble(MathFloor(lots / lotStep) * lotStep, 2);
   }

   limitPrice = NormalizeDouble(limitPrice, digits);

   double sl = 0, tp = 0;
   // Pip distances are measured from the limit price itself (the price the
   // order will fill at), mirroring how ExecuteTrade measures from ask/bid.
   if(slPips > 0) sl = NormalizeDouble(isLong ? limitPrice - PipsToPrice(symbol, slPips)
                                               : limitPrice + PipsToPrice(symbol, slPips), digits);
   if(tpPips > 0) tp = NormalizeDouble(isLong ? limitPrice + PipsToPrice(symbol, tpPips)
                                               : limitPrice - PipsToPrice(symbol, tpPips), digits);

   string comment = BuildOrderComment(baseComment, zoneId);

   datetime expiration = 0;
   ENUM_ORDER_TYPE_TIME timeType = ORDER_TIME_GTC;
   if(LimitOrderExpiryMin > 0)
   {
      timeType = ORDER_TIME_SPECIFIED;
      expiration = TimeCurrent() + LimitOrderExpiryMin * 60;
   }

   Log((isLong ? "BUY_LIMIT" : "SELL_LIMIT") + " @ " + DoubleToString(limitPrice, digits) +
       " | SL: " + DoubleToString(sl, digits) + " (" + DoubleToString(slPips, 1) + " pips)" +
       " | TP: " + DoubleToString(tp, digits) + " (" + DoubleToString(tpPips, 1) + " pips)" +
       " | zone=" + zoneSrc + "#" + IntegerToString((int)zoneId));

   bool ok;
   if(isLong)
      ok = trade.BuyLimit(lots, limitPrice, symbol, sl, tp, timeType, expiration, comment);
   else
      ok = trade.SellLimit(lots, limitPrice, symbol, sl, tp, timeType, expiration, comment);

   if(ok)
   {
      Log("OK: LIMIT ORDER ARMED! Ticket: " + IntegerToString((int)trade.ResultOrder()));
      g_limitOrdersArmed++;
      g_ackOrder = trade.ResultOrder();
      g_ackReqPrice = limitPrice;
      g_ackVolume = lots;
      g_ackRetcode = trade.ResultRetcode();
      return true;
   }

   g_ackRetcode = trade.ResultRetcode();
   Log("FAIL: ARM FAILED: " + RetcodeText());
   return false;
}

//+------------------------------------------------------------------+
//| [v3.2] Cancel a resting limit order for a direction                |
//|                                                                    |
//| Only deletes the pending order if its stamped zone_id matches -    |
//| a mismatch means a newer ARM already replaced it (this cancel      |
//| arrived late/out of order), so it's logged and skipped rather      |
//| than deleting the wrong (newer) order.                             |
//+------------------------------------------------------------------+
bool CancelLimitOrder(string symbol, bool isLong, long zoneId)
{
   if(symbol == "")
   {
      Log("ERROR: CANCEL signal missing symbol");
      return false;
   }

   ENUM_ORDER_TYPE orderType = isLong ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;
   int deleted = DeletePendingLimitOrders(symbol, orderType, zoneId, "cancelled by CANCEL_" + (isLong ? "LONG" : "SHORT"));

   if(deleted > 0)
   {
      g_limitOrdersCancelled += deleted;
      return true;
   }

   Log("SKIP: No matching resting " + (isLong ? "BUY_LIMIT" : "SELL_LIMIT") + " for " + symbol +
       " with zone #" + IntegerToString((int)zoneId) + " (already gone, or superseded by a newer ARM)");
   return false;
}

//+------------------------------------------------------------------+
//| [v3.2] Delete pending limit order(s) matching symbol/magic/type.  |
//| requiredZoneId >= 0 restricts deletion to an order whose stamped  |
//| zone_id (from its comment) matches exactly; pass -1 to delete     |
//| regardless of zone_id (used when a new ARM supersedes whatever    |
//| was resting). Returns the number of orders deleted.                |
//+------------------------------------------------------------------+
int DeletePendingLimitOrders(string symbol, ENUM_ORDER_TYPE orderType, long requiredZoneId, string reason)
{
   int deleted = 0;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;

      if(!IsOurMagic(OrderGetInteger(ORDER_MAGIC))) continue;
      if(OrderGetString(ORDER_SYMBOL) != symbol) continue;
      if((ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE) != orderType) continue;

      if(requiredZoneId >= 0)
      {
         long orderZoneId = ExtractZoneIdFromComment(OrderGetString(ORDER_COMMENT));
         if(orderZoneId != requiredZoneId)
         {
            Log("  Ticket " + IntegerToString((int)ticket) + " zone #" + IntegerToString((int)orderZoneId) +
                " != requested #" + IntegerToString((int)requiredZoneId) + " - leaving it alone");
            continue;
         }
      }

      if(trade.OrderDelete(ticket))
      {
         Log("OK: Deleted pending order ticket " + IntegerToString((int)ticket) + " (" + reason + ")");
         deleted++;
      }
      else
      {
         Log("FAIL: Failed to delete ticket " + IntegerToString((int)ticket) + ": " + RetcodeText());
      }
   }

   return deleted;
}

//+------------------------------------------------------------------+
//| [v3.2] Modify SL and/or TP on the open position for a symbol.     |
//| (Fast-Move TP Extension bracket-move alert.) hasSl/hasTp mark     |
//| which side actually changed - the other side is read back from    |
//| the position and re-sent unchanged, since PositionModify() needs  |
//| both values and an absent field must NOT be treated as "set to 0".|
//+------------------------------------------------------------------+
bool ModifyOpenPosition(string symbol, bool hasSl, double newSl, bool hasTp, double newTp)
{
   if(symbol == "")
   {
      Log("ERROR: MODIFY signal missing symbol");
      return false;
   }

   if(!hasSl && !hasTp)
   {
      Log("SKIP: MODIFY signal has neither sl_price nor tp_price - nothing to do");
      return false;
   }

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   // Pine's bracket covers its whole position, so after a double-down every
   // leg gets the new SL/TP (previously only the newest leg was modified).
   ulong tickets[];
   int n = CollectPositionsFifo(symbol, -1, tickets);
   int modified = 0, failed = 0;

   for(int i = 0; i < n; i++)
   {
      ulong ticket = tickets[i];
      if(!PositionSelectByTicket(ticket)) continue;

      double curSl = PositionGetDouble(POSITION_SL);
      double curTp = PositionGetDouble(POSITION_TP);

      double sl = hasSl ? NormalizeDouble(newSl, digits) : curSl;
      double tp = hasTp ? NormalizeDouble(newTp, digits) : curTp;

      Log("Modifying ticket " + IntegerToString((int)ticket) + " -> SL: " + DoubleToString(sl, digits) +
          (hasSl ? " (new)" : " (unchanged)") + "  TP: " + DoubleToString(tp, digits) + (hasTp ? " (new)" : " (unchanged)"));

      if(trade.PositionModify(ticket, sl, tp))
      {
         Log("OK: BRACKET MODIFIED! Ticket: " + IntegerToString((int)ticket));
         g_bracketsModified++;
         modified++;
      }
      else
      {
         g_ackRetcode = trade.ResultRetcode();
         Log("FAIL: MODIFY FAILED for ticket " + IntegerToString((int)ticket) + ": " + RetcodeText());
         failed++;
      }
   }

   if(n == 0)
   {
      Log("SKIP: No open position found for " + symbol + " to modify");
      return false;
   }
   return (failed == 0 && modified > 0);
}

//+------------------------------------------------------------------+
//| [v3.3] True if a position for symbol/magic/direction is open that |
//| was opened by a resting limit order (its comment carries the      |
//| "|zone_id" stamp from BuildOrderComment). Pine never sees those   |
//| fills, so it still sends its own bar-close BUY/SELL for the same  |
//| move; ProcessSignal uses this to skip that duplicate entry.        |
//+------------------------------------------------------------------+
bool LimitFilledPositionOpen(string symbol, bool isLong)
{
   ENUM_POSITION_TYPE wantType = isLong ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!IsOurMagic(PositionGetInteger(POSITION_MAGIC))) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != wantType) continue;
      if(ExtractZoneIdFromComment(PositionGetString(POSITION_COMMENT)) >= 0) return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| [v3.2] Stamp a zone_id onto an order comment, compactly (MT5      |
//| order comments are limited to ~31 chars): "TV-ZMQ|41821"          |
//+------------------------------------------------------------------+
string BuildOrderComment(string baseComment, long zoneId)
{
   return baseComment + "|" + IntegerToString((int)zoneId);
}

//+------------------------------------------------------------------+
//| [v3.2] Read a zone_id back out of an order comment built by       |
//| BuildOrderComment(). Returns -1 if the comment has no "|" marker. |
//+------------------------------------------------------------------+
long ExtractZoneIdFromComment(string comment)
{
   int pos = StringFind(comment, "|");
   if(pos < 0) return -1;
   string idStr = StringSubstr(comment, pos + 1);
   return (long)StringToInteger(idStr);
}

//+------------------------------------------------------------------+
//| Parse string value from JSON                                      |
//+------------------------------------------------------------------+
string ParseJsonString(string json, string key)
{
   string searchKey = "\"" + key + "\"";
   int keyPos = StringFind(json, searchKey);

   if(keyPos == -1) return "";

   int colonPos = StringFind(json, ":", keyPos);
   if(colonPos == -1) return "";

   int valueStart = colonPos + 1;
   while(valueStart < StringLen(json))
   {
      ushort c = StringGetCharacter(json, valueStart);
      if(c != ' ' && c != '\t' && c != '\n' && c != '\r') break;
      valueStart++;
   }

   if(StringGetCharacter(json, valueStart) == '"')
   {
      valueStart++;
      int valueEnd = StringFind(json, "\"", valueStart);
      if(valueEnd == -1) return "";
      return StringSubstr(json, valueStart, valueEnd - valueStart);
   }
   else
   {
      int valueEnd = valueStart;
      while(valueEnd < StringLen(json))
      {
         ushort c = StringGetCharacter(json, valueEnd);
         if(c == ',' || c == '}' || c == ']' || c == '\n' || c == '\r')
            break;
         valueEnd++;
      }
      string value = StringSubstr(json, valueStart, valueEnd - valueStart);
      StringTrimLeft(value);
      StringTrimRight(value);
      return value;
   }
}

//+------------------------------------------------------------------+
//| Parse double value from JSON                                      |
//+------------------------------------------------------------------+
double ParseJsonDouble(string json, string key)
{
   string value = ParseJsonString(json, key);
   if(value == "") return 0.0;
   return StringToDouble(value);
}

//+------------------------------------------------------------------+
//| [v3.2] True if key is present in the JSON AND its raw value isn't |
//| the literal null (Python's json.dumps(None) -> "null", unquoted). |
//| Needed anywhere "field absent" must be distinguished from "field  |
//| present with value 0" - e.g. MODIFY's sl_price/tp_price, which    |
//| can each independently be turned off via the script's alert       |
//| toggles and must leave that side of the bracket unchanged, not    |
//| get set to price 0.                                                |
//+------------------------------------------------------------------+
bool JsonHasNonNull(string json, string key)
{
   string raw = ParseJsonString(json, key);
   return (raw != "" && raw != "null");
}

//+------------------------------------------------------------------+
//| Logging function                                                  |
//+------------------------------------------------------------------+
void Log(string message)
{
   g_lastLog = message;   // [v3.5] reported as the ack's "detail"
   if(!EnableLogging) return;
   Print("[ZMQ] " + message);
}

//+------------------------------------------------------------------+
//| OnTick - Not used for this EA                                     |
//+------------------------------------------------------------------+
void OnTick()
{
   // Signal processing is handled by OnTimer via ZeroMQ
}
//+------------------------------------------------------------------+
