"""Decision parsing, prompts, budget/pricing and the DeepSeek adapter (against a local fake)."""

import json
from datetime import datetime, timezone

import pytest
from aiohttp import web

from jawn.agents.decision import decision_tool, parse_decision
from jawn.agents.prompt import SHARED_SYSTEM, build_messages
from jawn.config import AgentConfig, DeepSeekConfig, PricingConfig
from jawn.control.actions import ActionKind
from jawn.llm import pricing
from jawn.llm.base import DecisionRequest, ProviderError, Usage
from jawn.llm.budget import BudgetExceeded, BudgetMeter
from jawn.llm.deepseek import PAID_ENV, DeepSeekProvider

ALLOWED = [ActionKind.MACHINE_ENABLE, ActionKind.MACHINE_DISABLE]


def d(**kw):
    base = {"action": "enable", "target": "plate", "reason": "go", "message_to": "none", "message": "", "memory_update": "m"}
    base.update(kw)
    return json.dumps(base)


@pytest.mark.parametrize(
    "raw,ok,err",
    [
        (d(), True, None),
        (d(action="wait", target="none"), True, None),
        ("```json\n" + d() + "\n```", True, None),
        ("Sure! " + d() + " hope that helps", True, None),
        ("", False, "empty"),
        ("   \n ", False, "empty"),
        ("enable the plate machine", False, "not JSON"),
        (d(action="explode"), False, "unknown action"),
        (d(target="rod"), False, "not one of your machines"),
        (d(message_to="ghost_agent"), False, "not a known agent"),
        (json.dumps({"action": "enable", "target": "plate", "extra": 1}), False, "unexpected fields"),
        (d(action="switch_on"), False, "not allowed"),
        ("[1,2]", False, "not an object"),
    ],
)
def test_parse_decision(raw, ok, err):
    dec, e = parse_decision(raw, ALLOWED, ["plate"], ["rod_agent"])
    assert (dec is not None) == ok
    if err:
        assert err in e


def test_parse_truncates_long_fields():
    dec, _ = parse_decision(d(reason="x" * 999, memory_update="y" * 999, message_to="rod_agent", message="z" * 999), ALLOWED, ["plate"], ["rod_agent"])
    assert dec and dec.truncated and len(dec.reason) == 200 and len(dec.memory_update) == 300 and len(dec.message) == 200


def test_tool_schema_is_strict_compatible():
    t = decision_tool(ALLOWED, ["plate"], ["rod_agent"])
    f = t["function"]
    assert f["strict"] is True
    p = f["parameters"]
    assert p["additionalProperties"] is False and set(p["required"]) == set(p["properties"])
    assert p["properties"]["action"]["enum"] == ["disable", "enable", "wait"]
    assert p["properties"]["target"]["enum"] == ["plate", "none"]
    assert not any(k in json.dumps(p) for k in ("maxLength", "minLength", "minItems", "maxItems"))


def test_prompt_prefix_shared_and_stable():
    a = AgentConfig(id="a", role="r", objective="o", owns=["plate"])
    b = AgentConfig(id="b", role="r2", objective="o2", owns=["rod"])
    m1 = build_messages(a, ["b"], {"x": 1})
    m2 = build_messages(a, ["b"], {"x": 2, "t": 99})
    m3 = build_messages(b, ["a"], {"x": 1})
    assert m1[0] == m2[0], "system prompt must be byte-identical across calls for cache hits"
    assert m1[0]["content"].startswith(SHARED_SYSTEM) and m3[0]["content"].startswith(SHARED_SYSTEM)
    assert m1[1] != m2[1]
    assert "json" in build_messages(a, ["b"], {}, mode="json")[0]["content"].lower()  # JSON mode requirement


P = PricingConfig()
PEAK = datetime(2026, 11, 23, 2, 30, tzinfo=timezone.utc)  # Monday 02:30 UTC
OFF = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
WEEKEND = datetime(2026, 11, 28, 8, 0, tzinfo=timezone.utc)  # Saturday inside a peak window


def test_pricing_peak_schedule_and_cost():
    assert pricing.is_peak(PEAK, P) and not pricing.is_peak(OFF, P) and not pricing.is_peak(WEEKEND, P)
    u = Usage(prompt_tokens=2000, cache_hit_tokens=1800, cache_miss_tokens=200, completion_tokens=60)
    peak = pricing.cost(u, P, PEAK)
    assert peak == pytest.approx((1800 * 0.006 + 200 * 0.30 + 60 * 1.20) / 1e6)
    assert pricing.cost(u, P, OFF) == pytest.approx(peak / 2)
    assert pricing.worst_case(2000, 200, P) == pytest.approx((2000 * 0.30 + 200 * 1.20) / 1e6)


def test_budget_reservation_caps_and_separation():
    b = BudgetMeter(max_usd=0.001, simulated_max_usd=1.0, max_calls=100, max_calls_per_agent=3)
    r = b.reserve("a", 0.0006, simulated=False)
    with pytest.raises(BudgetExceeded):
        b.reserve("a", 0.0006, simulated=False)  # reservation counts before the first call returns
    b.commit("a", r, Usage(prompt_tokens=10, cost_usd=0.0002))
    assert b.real.spent_usd == pytest.approx(0.0002) and b.real.reserved_usd == 0
    b.reserve("a", 0.0006, simulated=True)  # simulated ledger is separate
    b.reserve("a", 0.0001, simulated=True)
    with pytest.raises(BudgetExceeded):
        b.reserve("a", 0.0, simulated=True)  # per-agent call cap (3)
    zero = BudgetMeter(0.0, 1.0, 10, 10)
    with pytest.raises(BudgetExceeded):
        zero.reserve("a", 1e-9, simulated=False)  # max_usd = 0 means no paid call at all
    b.hard_stop = "402"
    with pytest.raises(BudgetExceeded):
        b.reserve("z", 0.0, simulated=True)


# ----------------------------------------------------------------------------- DeepSeek fake
class FakeDeepSeek:
    def __init__(self):
        self.requests: list[dict] = []
        self.queue: list[tuple[int, object]] = []

    def app(self):
        app = web.Application()

        async def chat(request):
            self.requests.append({"path": request.path, "auth": request.headers.get("Authorization"), "body": await request.json()})
            status, payload = self.queue.pop(0) if self.queue else (200, ok_payload(d()))
            if isinstance(payload, str):
                return web.Response(status=status, text=payload)
            return web.json_response(payload, status=status)

        app.router.add_post("/beta/chat/completions", chat)
        app.router.add_post("/chat/completions", chat)
        return app


def ok_payload(args, tool=True, usage=None):
    msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "decide", "arguments": args}}]} if tool else {"role": "assistant", "content": args}
    return {"id": "x", "model": "deepseek-flash", "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}], "usage": usage or {"prompt_tokens": 1200, "prompt_cache_hit_tokens": 1024, "prompt_cache_miss_tokens": 176, "completion_tokens": 40, "total_tokens": 1240, "completion_tokens_details": {"reasoning_tokens": 0}}}


@pytest.fixture
async def fake(aiohttp_unused_port=None):
    f = FakeDeepSeek()
    runner = web.AppRunner(f.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    f.url = f"http://127.0.0.1:{port}"
    yield f
    await runner.cleanup()


def req():
    return DecisionRequest("plate_agent", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], decision_tool(ALLOWED, ["plate"], ["rod_agent"]), {}, 0, max_tokens=150)


def provider(url, mode="tools_strict", **kw):
    cfg = DeepSeekConfig(base_url=url, allow_paid=kw.pop("allow_paid", True), mode=mode, max_retries=kw.pop("max_retries", 2))
    return DeepSeekProvider(cfg, api_key="sk-test", now=lambda: PEAK, sleep=_nosleep)


async def _nosleep(_):
    return None


async def test_paid_guard_blocks_without_both_flags(fake, monkeypatch):
    monkeypatch.delenv(PAID_ENV, raising=False)
    p = provider(fake.url)
    with pytest.raises(ProviderError) as e:
        await p.complete(req())
    assert e.value.kind == "paid_calls_disabled" and p.sent == 0 and not fake.requests
    monkeypatch.setenv(PAID_ENV, "1")
    p2 = provider(fake.url, allow_paid=False)
    with pytest.raises(ProviderError):
        await p2.complete(req())
    assert not fake.requests
    await p.close()
    await p2.close()


async def test_request_shape_and_parsing(fake, monkeypatch):
    monkeypatch.setenv(PAID_ENV, "1")
    p = provider(fake.url)
    res = await p.complete(req())
    r = fake.requests[0]
    assert r["path"] == "/beta/chat/completions" and r["auth"] == "Bearer sk-test"
    b = r["body"]
    assert b["model"] == "deepseek-flash" and b["thinking"] == {"type": "disabled"} and b["max_tokens"] == 150
    assert b["tools"][0]["function"]["strict"] is True and b["tool_choice"]["function"]["name"] == "decide"
    assert json.loads(res.raw)["action"] == "enable"
    assert res.usage.cache_hit_tokens == 1024 and not res.usage.simulated
    assert res.usage.cost_usd == pytest.approx((1024 * 0.006 + 176 * 0.30 + 40 * 1.20) / 1e6)
    assert "sk-test" not in repr(p)
    await p.close()


async def test_json_mode_and_blank_content(fake, monkeypatch):
    monkeypatch.setenv(PAID_ENV, "1")
    p = provider(fake.url, mode="json")
    fake.queue.append((200, ok_payload("   ", tool=False)))
    res = await p.complete(req())
    assert fake.requests[0]["path"] == "/chat/completions" and fake.requests[0]["body"]["response_format"] == {"type": "json_object"}
    assert parse_decision(res.raw, ALLOWED, ["plate"], [])[1] == "empty output"
    await p.close()


async def test_error_mapping_and_retries(fake, monkeypatch):
    monkeypatch.setenv(PAID_ENV, "1")
    p = provider(fake.url, max_retries=2)
    fake.queue += [(503, "busy"), (429, "slow down"), (200, ok_payload(d()))]
    await p.complete(req())
    assert len(fake.requests) == 3
    fake.queue += [(402, {"error": {"message": "Insufficient Balance"}})]
    with pytest.raises(ProviderError) as e:
        await p.complete(req())
    assert e.value.kind == "budget" and len(fake.requests) == 4  # never retried
    fake.queue += [(401, "no")]
    with pytest.raises(ProviderError) as e2:
        await p.complete(req())
    assert e2.value.kind == "auth"
    fake.queue += [(500, "x"), (500, "x"), (500, "x")]
    with pytest.raises(ProviderError) as e3:
        await p.complete(req())
    assert e3.value.kind == "server" and e3.value.retryable
    fake.queue += [(200, "\n\n   " + json.dumps(ok_payload(d())))]  # keep-alive blank lines first
    assert json.loads((await p.complete(req())).raw)["target"] == "plate"
    await p.close()


def test_refuses_plain_http_remote():
    with pytest.raises(ValueError):
        DeepSeekProvider(DeepSeekConfig(base_url="http://api.deepseek.com"), api_key="k")


def test_estimate_is_upper_bound():
    p = DeepSeekProvider(DeepSeekConfig(), api_key="k")
    est = p.estimate_max_cost(req())
    assert 0 < est < 0.01
