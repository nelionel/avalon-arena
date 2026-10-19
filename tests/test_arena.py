"""Model players and the arena with stand-in models: every backend adapter, retries, failures, leaderboard.

Run: python3 tests/test_arena.py
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from avalon import arena, referee  # noqa: E402
from avalon.audit import audit_game  # noqa: E402
from avalon.drivers import model_player  # noqa: E402
from avalon.drivers.local_bot import BotError  # noqa: E402
from avalon.models import ModelError, Reply  # noqa: E402

THREE = ["haiku-5.5", "gpt-6-luna", "qwen-3.8-flash-next"]


class ScriptedChat:
    """A chat backend that replays fixed answers (then falls back to the stand-in model)."""
    answers: list = []

    def __init__(self, spec, base_url=None):
        self.spec = spec

    async def start(self, system, **_):
        self.system = system

    async def send(self, text):
        if ScriptedChat.answers:
            a = ScriptedChat.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return Reply(text=a, seconds=0.01)
        from avalon.arena_mock import mock_reply
        return Reply(text=mock_reply(text), seconds=0.01)

    async def stop(self):
        pass


class ArenaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avalon-arena-test-")
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(referee, "GAMES", self.root / "games"),
                        patch.object(referee, "SLOTS", self.root / "slots"),
                        patch.dict(os.environ, {})]
        for p in self.patches:
            p.start()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def play(self, seats, **kw):
        cfg = referee.GameConfig(**{"players": 6, "driver": "bots", "seats": seats, "voice": False, "seed": 2, **kw})
        r = referee.Referee(cfg)
        await r.run()
        return r

    async def test_every_backend_plays_a_full_game_against_stand_ins(self):
        seats = {"P1": "model:haiku-5.5", "P2": "model:gpt-6-luna", "P3": "model:qwen-3.8-flash-next",
                 "P4": "model:haiku-5.5-cli", "P5": "model:gpt-6-luna-api"}
        r = await self.play(seats, mock=True)
        s = json.loads((r.ref / "summary.json").read_text())
        self.assertIn(s["winner"], ("good", "evil"))
        self.assertEqual(s["bots"]["P1"], "Haiku 5.5")
        self.assertEqual(s["bots"]["P2"], "GPT-6 Luna")
        self.assertEqual(s["bots"]["P3"], "Qwen3.8 Flash Next")
        self.assertEqual(s["bots"]["P6"], "scripted")
        report = audit_game(r.dir)
        self.assertEqual(report["summary"]["failed"], [])
        for p in ("p1", "p2", "p3", "p4", "p5"):
            usage = (r.dir / "players" / p / "audit" / "usage.jsonl").read_text().splitlines()
            self.assertTrue(usage, p)
            transcript = (r.dir / "players" / p / "audit" / "codex-transcript.jsonl").read_text()
            self.assertIn("[system prompt]", transcript)
        roles = json.loads((r.ref / "roles.json").read_text())
        p1_transcript = (r.dir / "players" / "p1" / "audit" / "codex-transcript.jsonl").read_text()
        for p, role in roles.items():                 # a seat's prompts carry only its own role card
            if p != "P1" and role not in ("servant",) and role != roles["P1"]:
                self.assertNotIn(f"You are **{p}**", p1_transcript)

    @unittest.skipUnless(os.environ.get("AVALON_TEST_CLAUDE_BINARY"), "set AVALON_TEST_CLAUDE_BINARY to a claude executable")
    async def test_real_claude_cli_client_plays_a_game(self):
        """The real Claude Code client, talking to the stand-in Messages API, holds a seat for a whole game."""
        import threading
        from avalon.arena_mock import serve
        srv = serve(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        home = self.root / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_AGENT"))}
        env.update(HOME=str(home), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}",
                   ANTHROPIC_API_KEY="sk-ant-api03-mock-" + "0" * 40, DISABLE_AUTOUPDATER="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", AVALON_CLAUDE_BINARY=os.environ["AVALON_TEST_CLAUDE_BINARY"])
        try:
            with patch.dict(os.environ, env, clear=True):
                r = await self.play({"P1": "model:haiku-5.5-cli"}, players=5)
        finally:
            srv.shutdown()
            srv.server_close()
        s = json.loads((r.ref / "summary.json").read_text())
        self.assertEqual(s["bots"]["P1"], "Haiku 5.5")
        usage = (r.dir / "players" / "p1" / "audit" / "usage.jsonl").read_text().splitlines()
        self.assertGreater(len(usage), 5)

    @unittest.skipUnless(os.environ.get("AVALON_TEST_CODEX_BINARY"), "set AVALON_TEST_CODEX_BINARY to a codex executable")
    async def test_real_codex_client_plays_a_game(self):
        """The real Codex app-server, with the stand-in as its model provider, holds a GPT-6 Luna seat for a game.

        Codex's Linux sandbox needs user namespaces; where they are unavailable this test swaps the seat's
        permission profile for full access. The stand-in never runs tools, so nothing uses that access.
        """
        import threading
        from avalon.arena_mock import serve
        from avalon.drivers import codex_rpc
        srv = serve(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        home = self.root / "home"
        (home / ".codex").mkdir(parents=True)
        real_exec = codex_rpc.asyncio.create_subprocess_exec

        async def launch(*argv, **kw):
            argv, out = list(argv), []
            while argv:
                a = argv.pop(0)
                if a == "-c" and argv and argv[0].startswith(("permissions.", "default_permissions=")):
                    argv.pop(0)
                    continue
                out.append(a)
            out += ["-c", 'model_provider="standin"', "-c", 'sandbox_mode="danger-full-access"', "-c",
                    f'model_providers.standin={{name="standin", base_url="http://127.0.0.1:{port}/v1", '
                    f'env_key="STANDIN_KEY", wire_api="responses"}}']
            return await real_exec(*out, **kw)

        real_call = codex_rpc.CodexRPC.call

        async def call(rpc, method, params, timeout=90):
            if method in ("thread/start", "turn/start"):
                params = {k: v for k, v in params.items() if k != "permissions"}
            return await real_call(rpc, method, params, timeout)

        env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"), "STANDIN_KEY": "standin",
               "AVALON_CODEX_BINARY": os.environ["AVALON_TEST_CODEX_BINARY"]}
        try:
            with patch.dict(os.environ, env, clear=True), patch.object(codex_rpc.asyncio, "create_subprocess_exec", launch), \
                    patch.object(codex_rpc.CodexRPC, "call", call):
                r = await self.play({"P1": "model:gpt-6-luna"}, players=5)
        finally:
            srv.shutdown()
            srv.server_close()
        s = json.loads((r.ref / "summary.json").read_text())
        self.assertEqual(s["bots"]["P1"], "GPT-6 Luna")
        usage = (r.dir / "players" / "p1" / "audit" / "usage.jsonl").read_text().splitlines()
        self.assertGreater(len(usage), 5)

    async def test_relay_seats_are_answered_through_files(self):
        """A relay seat (as played by Claude Code subagents) exchanges one request and one reply file per turn."""
        from avalon import models
        from avalon.arena_mock import mock_reply
        relay = self.root / "relay"
        stop = asyncio.Event()

        async def answer():                      # stands in for the orchestrator and its subagents
            while not stop.is_set():
                for req in models.pending_relay(relay):
                    text = req.read_text()
                    self.assertIn("THIS TURN", text)
                    self.assertIn("Do not open, list or search any other file", text)
                    req.with_name(req.name.replace(".request.md", ".reply.txt")).write_text(mock_reply(text))
                await asyncio.sleep(0.05)

        task = asyncio.create_task(answer())
        try:
            with patch.object(models, "relay_dir", lambda spec: relay):
                r = await self.play({"P1": "model:haiku-5.5-subagent", "P2": "model:haiku-5.5-subagent"}, players=5)
        finally:
            stop.set()
            await task
        s = json.loads((r.ref / "summary.json").read_text())
        self.assertEqual((s["bots"]["P1"], s["bots"]["P2"]), ("Haiku 5.5", "Haiku 5.5"))
        answered = sorted(relay.glob("*-p1-*.answered.md"))
        self.assertGreater(len(answered), 5)
        later = answered[-1].read_text()
        self.assertIn("### You replied", later)             # each request carries the seat's whole conversation
        roles = json.loads((r.ref / "roles.json").read_text())
        self.assertNotIn(f"You are **P2**", later)          # and only this seat's own role card

    async def test_real_models_are_refused_without_credentials(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY",)}
        with patch.dict(os.environ, env, clear=True), patch("avalon.models.CREDENTIALS", self.root / "none.env"):
            with self.assertRaises(BotError) as e:
                await self.play({"P1": "model:haiku-5.5"})
        self.assertIn("needs ANTHROPIC_API_KEY", str(e.exception))
        self.assertFalse((self.root / "games").exists() and any((self.root / "games").iterdir()))

    async def test_invalid_reply_gets_corrected_then_accepted(self):
        ScriptedChat.answers = ["I approve, sounds fine.", '{"ack": false}', '{"ack": true, "reason": "ready"}']
        with patch.dict(model_player.__dict__, {"make_chat": lambda spec, base_url=None: ScriptedChat(spec)}):
            r = await self.play({"P1": "model:qwen-3.8-flash-next"}, mock=True, max_turns=1)
        usage = [json.loads(x) for x in (r.dir / "players" / "p1" / "audit" / "usage.jsonl").read_text().splitlines()]
        self.assertEqual([u["attempt"] for u in usage], [1, 2, 3])
        hooks = (r.dir / "players" / "p1" / "audit" / "hooks.jsonl").read_text()
        self.assertIn("no JSON object found", hooks)
        self.assertIn('\\"ack\\" must be true', hooks)

    async def test_three_bad_replies_stop_the_turn_unpublished(self):
        ScriptedChat.answers = ["no", "still no", "{}"]
        with patch.dict(model_player.__dict__, {"make_chat": lambda spec, base_url=None: ScriptedChat(spec)}):
            with self.assertRaises(RuntimeError) as e:
                await self.play({"P1": "model:qwen-3.8-flash-next"}, mock=True, game_id="bad")
        self.assertIn("P1 (Qwen3.8 Flash Next)", str(e.exception))
        self.assertEqual((self.root / "games" / "bad" / "public" / "events.jsonl").read_text(), "")

    async def test_unreachable_model_stops_the_game(self):
        ScriptedChat.answers = [ModelError("Qwen3.8 Flash Next: cannot reach http://127.0.0.1:8000/v1")]
        with patch.dict(model_player.__dict__, {"make_chat": lambda spec, base_url=None: ScriptedChat(spec)}):
            with self.assertRaises(RuntimeError) as e:
                await self.play({"P1": "model:qwen-3.8-flash-next"}, mock=True)
        self.assertIn("cannot reach", str(e.exception))

    async def test_arena_series_balances_seats_and_ranks_models(self):
        tag, gids = await arena.run_arena(THREE, 6, 3, seed=4, tag="t", mock=True, max_rounds=2)
        self.assertEqual(tag, "t")
        self.assertEqual(len(gids), 3)
        lb = arena.compute_leaderboard(self.root / "games", tag="t", include_mock=True)
        self.assertEqual(lb["games"], 3)
        self.assertEqual({r["label"]: r["seats"] for r in lb["players"]},
                         {"Haiku 5.5": 6, "GPT-6 Luna": 6, "Qwen3.8 Flash Next": 6})
        for r in lb["players"]:
            self.assertEqual(r["good"]["seats"] + r["evil"]["seats"], 6)
            self.assertTrue(r["evil_read"] is None or -1 <= r["evil_read"] <= 1)
        costs = {r["label"]: r["cost_per_seat_game"] for r in lb["players"]}
        self.assertGreater(costs["Qwen3.8 Flash Next"], 0)          # the OpenAI-style stand-in reports cost
        self.assertIsNone(costs["GPT-6 Luna"])                       # Codex reports none
        self.assertEqual(arena.compute_leaderboard(self.root / "games", tag="t")["games"], 0)   # dry runs hidden

    async def test_arena_huddle_rounds_let_evil_answer_each_other(self):
        class GoodWinsChat(ScriptedChat):           # approves every team and passes every quest: Good wins three
            async def send(self, text):
                from avalon.arena_mock import mock_reply
                move = model_player.extract_json(mock_reply(text))
                move.update({"vote": "approve"} if "vote" in move else {"card": "success"} if "card" in move else {})
                return Reply(text=json.dumps(move), seconds=0.01)

        with patch.dict(model_player.__dict__, {"make_chat": lambda spec, base_url=None: GoodWinsChat(spec)}):
            _, gids = await arena.run_arena(["qwen-3.8-flash-next"], 6, 1, seed=3, tag="h", mock=True, max_rounds=1,
                                            huddle_rounds=2)
        d = self.root / "games" / gids[0]
        self.assertEqual(json.loads((d / "referee" / "status.json").read_text())["config"]["huddle_rounds"], 2)
        kinds = [json.loads(x)["kind"] for x in (d / "referee" / "turns.jsonl").read_text().splitlines()]
        self.assertEqual(kinds.count("evil_huddle"), 2)
        self.assertEqual(kinds[kinds.index("evil_huddle") + 2], "assassinate")
        assassin = next(p for p, r in json.loads((d / "referee" / "roles.json").read_text()).items() if r == "assassin")
        lines = [json.loads(x) for x in (d / "players" / assassin.lower() / "audit" / "codex-transcript.jsonl")
                 .read_text().splitlines()]
        second = next(x["message"]["content"] for x in lines if x["type"] == "user" and "round 2 of 2" in x["message"]["content"])
        self.assertIn("Mock huddle thought.", second.split("Huddle so far:")[1])     # round 1's messages, to answer

    async def test_arena_deals_a_chosen_role_list(self):
        """The 8-player baseline: Merlin, Percival, 3 Servants against Morgana, Assassin and Oberon."""
        canon = ["merlin", "percival", "servant", "servant", "servant", "morgana", "assassin", "oberon"]
        _, gids = await arena.run_arena(["haiku-5.5-or", "gpt-6-luna-or"], 8, 1, seed=2, tag="canon", mock=True,
                                        max_rounds=1, roles=canon)
        d = self.root / "games" / gids[0]
        roles = json.loads((d / "referee" / "roles.json").read_text())
        self.assertEqual(sorted(roles.values()), sorted(canon))
        seat = {r: p for p, r in roles.items()}

        def card(role):                         # the seat's system prompt, as its transcript recorded it
            first = (d / "players" / seat[role].lower() / "audit" / "codex-transcript.jsonl").read_text().splitlines()[0]
            return json.loads(first)["message"]["content"]

        self.assertIn(seat["oberon"], card("merlin").split("These players are Evil:")[1].splitlines()[0])
        self.assertNotIn(seat["oberon"], card("assassin").split("Your Evil teammates:")[1].splitlines()[0])
        self.assertIn("Oberon is also Evil but hidden from you", card("morgana"))
        self.assertIn("You do not know who your Evil teammates are", card("oberon"))
        for bad in (canon[:-1] + ["servant"], canon[:5]):       # two Evil roles; too few roles
            with self.assertRaises(ValueError):
                await arena.run_arena(["scripted"], 8, 1, tag="bad", mock=True, roles=bad)

    async def test_arena_checks_every_entrant_before_playing(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True), patch("avalon.models.CREDENTIALS", self.root / "none.env"):
            with self.assertRaises(BotError) as e:
                await arena.run_arena(["scripted", "haiku-5.5"], 6, 2, tag="x")
        self.assertIn("cannot seat Haiku 5.5", str(e.exception))
        self.assertFalse((self.root / "games").exists() and any((self.root / "games").iterdir()))

    async def test_bots_join_an_arena_series(self):
        await arena.run_arena(["qwen-3.8-flash-next", str(ROOT / "bots" / "example_bot.py"), "scripted"], 6, 2,
                              seed=1, tag="mixed", mock=True, max_rounds=1)
        lb = arena.compute_leaderboard(self.root / "games", tag="mixed", include_mock=True)
        self.assertEqual({r["label"]: r["seats"] for r in lb["players"]},
                         {"Qwen3.8 Flash Next": 4, "example": 4, "scripted": 4})

    async def test_missing_backend_points_to_a_ready_one_for_the_same_model(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["AVALON_CLAUDE_BINARY"] = str(ROOT / "avalon" / "mock_clis" / "claude")
        with patch.dict(os.environ, env, clear=True), patch("avalon.models.CREDENTIALS", self.root / "none.env"):
            with self.assertRaises(BotError) as e:
                await self.play({"P1": "model:haiku-5.5"})
        self.assertIn("ready here as haiku-5.5-cli", str(e.exception))


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    """The OpenAI-compatible backend against a stand-in that answers the way OpenRouter does."""

    async def asyncSetUp(self):
        import threading
        from avalon import arena_mock, models
        self.mock, self.models = arena_mock, models
        asyncio.get_running_loop().slow_callback_duration = 10
        self.srv = arena_mock.serve(0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/v1"
        arena_mock.REQUESTS.clear()
        arena_mock.SCRIPTED.clear()
        self.patches = [patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}),
                        patch.object(models.HTTPChat, "DELAYS", (0, 0, 0))]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.srv.shutdown()
        self.srv.server_close()

    async def chat(self, key):
        from dataclasses import replace
        from avalon.models import get_model, make_chat
        spec = get_model(key)
        chat = make_chat(replace(spec, base_url=self.url))
        await chat.start("RULES", workdir=Path("/x/games/g1/players/p2/work"))
        return spec, chat

    def facts(self):
        return 'Turn facts:\n```json\n{"kind": "vote", "turn": 5, "you": "P2", "players": ["P1", "P2", "P3", "P4", "P5"]}\n```'

    async def test_request_shape_and_usage(self):
        from unittest.mock import PropertyMock
        from avalon.models import OpenAIChat
        with patch.object(OpenAIChat, "openrouter", new_callable=PropertyMock, return_value=True):
            for key, cached in [("haiku-5.5-or", True), ("gpt-6-luna-or", False), ("qwen-3.8-flash-or", True)]:
                spec, chat = await self.chat(key)
                reply = await chat.send(self.facts())
                path, headers, body = self.mock.REQUESTS[-1]
                self.assertEqual(path, "/api/v1/chat/completions")
                self.assertEqual(headers.get("Authorization") or headers.get("authorization"), "Bearer sk-or-test")
                self.assertEqual(body["model"], spec.model)
                self.assertEqual(body["reasoning"], {"effort": "medium"})
                self.assertEqual(body["session_id"], "avalon-g1-p2")
                self.assertEqual(body["max_tokens"], 16000)
                marked = [m for m in body["messages"] if isinstance(m["content"], list)]
                self.assertEqual(len(marked), 2 if cached else 0, key)
                if cached:
                    self.assertEqual(marked[0]["content"][0]["cache_control"], {"type": "ephemeral"})
                    self.assertEqual(body["messages"][0]["role"], "system")
                self.assertIn('"vote"', reply.text)
                self.assertEqual(reply.reasoning, "Mock reasoning.")
                self.assertGreater(reply.usage["cost"], 0)
                self.assertEqual(reply.usage["reasoning"], 3)
                self.assertEqual(reply.usage["provider"], "Stand-in")
                if key == "qwen-3.8-flash-or":
                    self.assertEqual((body["temperature"], body["top_k"]), (1.0, 20))
                else:
                    self.assertNotIn("temperature", body)

    async def test_session_id_only_for_openrouter(self):
        spec, chat = await self.chat("qwen-3.8-flash-or")      # base_url is the local stand-in, not openrouter.ai
        await chat.send(self.facts())
        self.assertNotIn("session_id", self.mock.REQUESTS[-1][2])

    async def test_mid_generation_error_in_http_200_is_retried(self):
        self.mock.SCRIPTED.append((200, {"error": {"code": 502, "message": "upstream died"}}, {}))
        self.mock.SCRIPTED.append((200, {"choices": [{"finish_reason": "error", "message": {"content": ""}}]}, {}))
        spec, chat = await self.chat("haiku-5.5-or")
        reply = await chat.send(self.facts())
        self.assertIn('"vote"', reply.text)
        self.assertEqual(len(self.mock.REQUESTS), 3)
        self.assertEqual(len(chat.messages), 2)                  # one user turn, one answer: retries leave no trace

    async def test_rate_limit_honours_retry_after(self):
        self.mock.SCRIPTED.append((429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "0"}))
        spec, chat = await self.chat("gpt-6-luna-or")
        reply = await chat.send(self.facts())
        self.assertIn('"vote"', reply.text)

    async def test_fatal_errors_stop_with_the_reason(self):
        cases = [(402, {"error": {"code": 402, "message": "Insufficient credits"}}, {}, "out of credits"),
                 (401, {"error": {"code": 401, "message": "No auth"}}, {}, "check the API key"),
                 (200, {"error": {"code": 403, "message": "flagged by moderation"}}, {}, "flagged by moderation")]
        for status, body, headers, expect in cases:
            self.mock.SCRIPTED.append((status, body, headers))
            spec, chat = await self.chat("haiku-5.5-or")
            with self.assertRaises(self.models.ModelError) as e:
                await chat.send(self.facts())
            self.assertIn(expect, str(e.exception))
            self.assertEqual(chat.messages, [])

    async def test_cut_off_reply_is_reported_as_such(self):
        self.mock.SCRIPTED.append((200, {"choices": [{"finish_reason": "length", "message": {"content": None,
                                         "reasoning": "long thoughts"}}], "usage": {"cost": 0.002}}, {}))
        spec, chat = await self.chat("qwen-3.8-flash-or")
        reply = await chat.send(self.facts())
        self.assertEqual((reply.text, reply.finish, reply.usage["cost"]), ("", "length", 0.002))


class PingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_reaches_each_backend(self):
        import threading
        from avalon.arena_mock import serve
        from avalon.models import get_model, ping
        srv = serve(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        env = {"ANTHROPIC_API_KEY": "sk-ant-mock", "AVALON_CODEX_BINARY": str(ROOT / "avalon" / "mock_clis" / "codex"),
               "AVALON_CLAUDE_BINARY": str(ROOT / "avalon" / "mock_clis" / "claude")}
        try:
            with patch.dict(os.environ, env):
                for key, base in [("haiku-5.5", url), ("qwen-3.8-flash-next", url + "/v1"), ("gpt-6-luna", None),
                                  ("haiku-5.5-cli", None)]:
                    ok, detail = await ping(get_model(key), base)
                    self.assertTrue(ok, f"{key}: {detail}")
        finally:
            srv.shutdown()
            srv.server_close()


class LauncherTests(unittest.TestCase):
    """The dashboard's match launcher: same-origin page requests only, configured models and repo bots only."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from avalon.dashboard import server
        self.server_mod = server
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body, **headers):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + "/api/arena", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_refuses_unsafe_requests(self):
        ok = {"models": ["scripted"], "mock": True}
        self.assertEqual(self.post(ok)[0], 403)                                             # no custom header
        self.assertEqual(self.post(ok, **{"x-avalon": "1", "origin": "http://evil.example"})[0], 403)
        for bad in (["bots/../../etc/passwd.py"], ["/tmp/evil.py"], ["no-such-model"], []):
            self.assertEqual(self.post({"models": bad, "mock": True}, **{"x-avalon": "1"})[0], 400, bad)
        self.assertEqual(self.post({"models": ["scripted"], "players": 11}, **{"x-avalon": "1"})[0], 400)
        self.assertEqual(self.post({"models": ["scripted"], "parallel": 50}, **{"x-avalon": "1"})[0], 400)

    def test_starts_a_series_with_models_and_repo_bots(self):
        tmp = tempfile.TemporaryDirectory(prefix="avalon-launcher-test-")
        self.addCleanup(tmp.cleanup)
        with patch.object(self.server_mod.subprocess, "Popen") as popen, \
                patch.object(self.server_mod.data, "GAMES", Path(tmp.name)):
            code, out = self.post({"models": ["qwen-3.8-flash-next", "bots/example_bot.py", "scripted"], "mock": True,
                                   "players": 6, "games": 2, "parallel": 2, "huddle_rounds": 2},
                                  **{"x-avalon": "1", "origin": self.base})
        self.assertEqual(code, 200, out)
        argv = popen.call_args.args[0]
        self.assertIn("qwen-3.8-flash-next,bots/example_bot.py,scripted", argv)
        self.assertIn("--mock", argv)
        self.assertEqual(argv[argv.index("--parallel") + 1], "2")
        self.assertEqual(argv[argv.index("--huddle-rounds") + 1], "2")
        self.assertEqual(popen.call_args.kwargs["env"]["AVALON_CWD"], str(ROOT))


class UnitTests(unittest.TestCase):
    def test_extract_json_takes_the_last_object(self):
        text = 'Thinking {"draft": 1}\n```json\n{"vote": "approve", "beliefs": {"P2": 0.5}}\n```\nDone.'
        self.assertEqual(model_player.extract_json(text), {"vote": "approve", "beliefs": {"P2": 0.5}})
        self.assertIsNone(model_player.extract_json("no json here"))

    def test_arena_cli_passes_huddle_rounds_and_roles(self):
        from unittest.mock import AsyncMock
        from avalon import cli
        with patch.object(arena, "run_arena", AsyncMock(return_value=("t", []))) as run:
            cli.main(["arena", "--models", "scripted", "--mock", "--huddle-rounds", "2", "--roles", "merlin,assassin"])
        self.assertEqual(run.call_args.kwargs["huddle_rounds"], 2)
        self.assertEqual(run.call_args.kwargs["roles"], ["merlin", "assassin"])

    def test_unknown_arena_entrant_is_refused(self):
        with self.assertRaises(Exception) as e:
            arena.seatings(["no-such-model"], 6, 1, seed=1)
        self.assertIn("unknown model", str(e.exception))

    def test_seatings_share_seats_evenly(self):
        for players in range(5, 11):
            for plan in arena.seatings(THREE, players, 3, seed=1):
                counts = Counter(plan.values())
                self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
                self.assertEqual(len(plan), players)

    def test_every_public_event_reads_as_text(self):
        events = [{"type": "game_started", "leader": "P1", "team_size": 2},
                  {"type": "proposal", "quest": 1, "proposal": 1, "leader": "P1", "team": ["P1", "P2"], "message": "hi"},
                  {"type": "discussion", "round": 1, "entries": [{"player": "P1", "message": None, "ready": True}]},
                  {"type": "vote_result", "team": ["P1"], "approved": True, "votes": {"P1": "approve"}},
                  {"type": "quest_result", "quest": 1, "team": ["P1"], "success": False, "fails": 1,
                   "score": {"good": 0, "evil": 1}},
                  {"type": "assassination", "assassin": "P3", "target": "P1", "message": None, "hit": True}]
        for e in events:
            self.assertTrue(model_player.describe(e))


if __name__ == "__main__":
    unittest.main()
