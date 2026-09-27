"""The alert strings below are verbatim shapes built by the IGT Pine script
(constructAlertMessage / constructLimitArmMessage / constructLimitCancelMessage
/ constructExitMessage / buildPreExitAlert and the FTP modify alert)."""
import pytest

from app import server, translate, zmq_client
from app.parser import parse_alert


def tr(body):
    return translate.translate(parse_alert(body))


def test_arm_long():
    sig = tr(
        "signal=armlong,symbol=USDJPY,qty=12000,order_type=limit,limit_price=147.250,"
        "sl_price=147.050,tp_price=147.650,zone_id=41821,zone_src=OB"
    )
    assert sig["action"] == "ARM_LONG"
    assert sig["symbol"] == "USDJPY"
    assert sig["size"] == pytest.approx(0.12)
    assert sig["limit_price"] == 147.25
    assert sig["sl_pips"] == pytest.approx(20.0)
    assert sig["tp_pips"] == pytest.approx(40.0)
    assert sig["zone_id"] == 41821
    assert sig["zone_src"] == "OB"
    assert sig["action"] in translate.VALID_ACTIONS


def test_arm_short_without_sl():
    # Default Pine settings omit sl_price ("Include SL Price" is off).
    sig = tr(
        "signal=armshort,symbol=EURUSD,qty=1000,order_type=limit,limit_price=1.08500,"
        "tp_price=1.08100,zone_id=5120,zone_src=FVG,strategy=ictgann"
    )
    assert sig["action"] == "ARM_SHORT"
    assert sig["sl_pips"] == 0.0
    assert sig["tp_pips"] == pytest.approx(40.0)
    assert sig["zone_src"] == "FVG"


def test_arm_without_limit_price_is_rejected():
    with pytest.raises(ValueError):
        tr("signal=armlong,symbol=EURUSD,qty=1000,zone_id=1,zone_src=OB")


@pytest.mark.parametrize("signal,action", [("cancellong", "CANCEL_LONG"), ("cancelshort", "CANCEL_SHORT")])
def test_cancel(signal, action):
    sig = tr(f"signal={signal},symbol=EURUSD,order_type=limit,zone_id=41821,zone_src=iFVG")
    assert sig == {**sig, "action": action, "symbol": "EURUSD", "zone_id": 41821, "zone_src": "iFVG"}
    assert action in translate.VALID_ACTIONS


def test_market_entry_carries_zone_and_dd_flags():
    sig = tr(
        "signal=long,symbol=EURUSD,qty=3000,entry_price=1.08500,tp_price=1.08900,"
        "conf_pd=true,entry_src=zone,tp_atr=2.5,sl_atr=1.5,entry_mode=market,zone_id=77,zone_src=OB"
    )
    assert (sig["action"], sig["zone_id"], sig["dd"]) == ("BUY", 77, 0)
    assert sig["size"] == pytest.approx(0.03)
    assert sig["tp_pips"] == pytest.approx(40.0)

    dd = tr("signal=short,symbol=EURUSD,qty=3000,entry_price=1.08500,tp_price=1.08300,dd=1,entry_src=zone")
    assert (dd["action"], dd["zone_id"], dd["dd"]) == ("SELL", -1, 1)


@pytest.mark.parametrize(
    "body,action",
    [
        ("signal=closelong,symbol=EURUSD,close_price=1.08000,exit_reason=stop_loss", "CLOSELONG"),
        ("signal=closeallshort,symbol=EURUSD,close_price=1.08000,exit_reason=dd_recovery_tp", "CLOSESHORT"),
        (
            "signal=closeshort,symbol=EURUSD,close_price=1.08,avg_price=1.081,exit_reason=scored_vidya_4",
            "CLOSESHORT",
        ),
    ],
)
def test_exits(body, action):
    assert tr(body)["action"] == action


def test_modify():
    sig = tr("signal=modify,symbol=EURUSD,tp_price=1.09100,zone_id=77,zone_src=OB,ext=1/3")
    assert sig["action"] == "MODIFY"
    assert sig["tp_price"] == 1.091
    assert sig["sl_price"] is None


@pytest.fixture
def client(monkeypatch):
    sent = []
    monkeypatch.setattr(zmq_client, "send", lambda s: (sent.append(s), (True, 0.1))[1])
    c = server.app.test_client()
    c.sent = sent
    return c


ARM = "signal=armlong,symbol=EURUSD,qty=1000,order_type=limit,limit_price=1.08,zone_id=1,zone_src=OB"


def test_secret_in_url_query(client):
    # alert() messages can't carry secret= in the body; the URL must work.
    r = client.post("/webhook?secret=test-secret", data=ARM)
    assert r.status_code == 200
    assert client.sent[0]["action"] == "ARM_LONG"


def test_secret_in_body_still_works(client):
    r = client.post("/webhook", data=ARM + ",secret=test-secret")
    assert r.status_code == 200


@pytest.mark.parametrize("url", ["/webhook", "/webhook?secret=wrong"])
def test_bad_or_missing_secret_rejected(client, url):
    r = client.post(url, data=ARM)
    assert r.status_code == 401
    assert client.sent == []
