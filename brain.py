"""Brain: turns what the user said into tool calls + a short spoken reply.

Providers (config brain.provider): openai (Responses API) | gemini (google-genai).
FakeBrain is a no-AI test brain for --fake-brain.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from datetime import datetime

import actions
from safety import log

SYSTEM_PROMPT = """You are Jarvis, a voice assistant living on {user}'s Windows 11 PC.
Talk like a relaxed, friendly human assistant: warm, natural, a little witty, never robotic or stiff.
Address the user as "{address}" now and then, naturally (e.g. "Done, {address}." or "Right away, {address}."), not in every sentence.
Your replies are spoken aloud by text-to-speech, so:
- Reply in one or two short sentences (under about 15 words each) of plain conversational English. No markdown, lists, emojis or code.
- Never read out long file paths; say just the file or folder name.
- Don't end every reply with an offer like "anything else?" — only ask a question when you need an answer.

Understand casual, indirect requests and just act on harmless ones without asking, for example:
- "I feel like listening to music" / "play some music" -> open_app("{music_app}")
- "it's too loud" -> volume down; "I can't hear anything" -> volume up
- "I'm stepping away" -> lock_computer
- "look up how to make jollof" -> open_website with a search
- "clean up my desktop" -> organize_desktop

You can ONLY act on the PC through the provided tools. You cannot run shell commands or anything else.
- Never claim you did something unless a tool result says it succeeded. If a tool reports an error, is blocked, or was cancelled, say so briefly.
- delete_item, organize_desktop, and moving/renaming folders ask the user for confirmation themselves. Call the tool directly; do NOT ask "are you sure" yourself.
- Paths may start with desktop, documents, downloads, pictures, music, videos or home, e.g. "desktop/JarvisTest/notes.txt". If you don't know where a file is, use find_files first.
- For window commands, pass a short app name or title fragment, e.g. "chrome".
- If the request is genuinely ambiguous or missing information, ask one short question (ending with a question mark).
- For general questions or chit-chat that need no tool, just answer briefly and naturally.
- The input comes from speech recognition and may contain small mistakes; guess the most likely meaning.
Current local time: {now}."""

CANCEL_PHRASES = ("never mind", "nevermind", "cancel", "forget it", "stop", "that's all", "nothing")


def is_cancel(text: str) -> bool:
    t = re.sub(r"[^a-z' ]", "", (text or "").lower()).strip()
    return t in CANCEL_PHRASES or t.startswith(("never mind", "nevermind", "cancel that", "forget it"))


_MD = re.compile(r"[*_`#>]+|^\s*[-•]\s+", re.M)


_SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def clean_for_speech(text: str) -> str:
    return re.sub(r"\s+", " ", _MD.sub("", (text or "").translate(_SMART))).strip()


class BrainError(Exception):
    """A user-facing failure; the message is short and speakable."""


# ================================================================ base

class _Brain:
    def __init__(self, cfg: dict):
        b = cfg["brain"]
        self.max_rounds = int(b["max_tool_rounds"])
        self.memory_turns = int(b["memory_turns"])
        self.idle_reset = float(b["memory_reset_idle_s"])
        self.timeout = float(b["request_timeout_s"])
        self.temperature = b.get("temperature")
        prefs = cfg["assistant"]
        self.user = prefs.get("user_name") or "the user"
        self.music_app = prefs.get("music_app") or "spotify"
        self.address = prefs.get("address") or "sir"
        self.turns: list[list] = []  # each turn = provider-native message items
        self.last_used = 0.0
        self.last_error = False  # True if the last handle() failed (lets the HUD show ERROR)

    def reset(self) -> None:
        self.turns = []

    def _history(self) -> list:
        if time.monotonic() - self.last_used > self.idle_reset:
            self.turns = []
        return [item for turn in self.turns[-self.memory_turns:] for item in turn]

    def _system(self) -> str:
        return SYSTEM_PROMPT.format(user=self.user, music_app=self.music_app, address=self.address,
                                    now=datetime.now().strftime("%A %d %B %Y, %I:%M %p"))

    def handle(self, text: str) -> str:
        """Process one user utterance. Never raises; returns a speakable reply."""
        history = self._history()
        self.last_error = False
        try:
            reply, turn = self._run(text, history)
        except BrainError as e:
            log(f"BRAIN ERROR: {e}")
            self.last_error = True
            return str(e)
        except Exception as e:  # noqa: BLE001
            log(f"BRAIN ERROR (unexpected): {e!r}")
            self.last_error = True
            return "Sorry, something went wrong talking to the AI."
        self.turns.append(turn)
        self.turns = self.turns[-self.memory_turns:]
        self.last_used = time.monotonic()
        reply = clean_for_speech(reply) or "Done."
        log(f"USER: {text}  |  JARVIS: {reply}")
        return reply

    def _run(self, text: str, history: list) -> tuple[str, list]:
        raise NotImplementedError


def _tool_call(name: str, args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            args = {}
    return actions.run_tool(name, args or {})


# ================================================================ OpenAI

class OpenAIBrain(_Brain):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import openai
        if not os.environ.get("OPENAI_API_KEY"):
            raise BrainError("The OpenAI API key isn't set.")
        self.openai = openai
        self.client = openai.OpenAI(timeout=self.timeout, max_retries=2)
        self.model = cfg["brain"]["openai_model"]
        self.tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["parameters"]} for t in actions.TOOLS]

    def _create(self, items: list):
        # No temperature: current OpenAI models reject it.
        return self.client.responses.create(
            model=self.model, instructions=self._system(), input=items,
            tools=self.tools, store=False, max_output_tokens=400)

    def _run(self, text, history):
        o = self.openai
        turn: list = [{"role": "user", "content": text}]
        try:
            for _ in range(self.max_rounds):
                resp = self._create(history + turn)
                calls = [it for it in resp.output if it.type == "function_call"]
                if not calls:
                    reply = resp.output_text or ""
                    turn.append({"role": "assistant", "content": reply})
                    return reply, turn
                for c in calls:
                    turn.append({"type": "function_call", "call_id": c.call_id,
                                 "name": c.name, "arguments": c.arguments})
                    turn.append({"type": "function_call_output", "call_id": c.call_id,
                                 "output": _tool_call(c.name, c.arguments)})
            return "I couldn't finish that in a reasonable number of steps.", turn
        except o.AuthenticationError:
            raise BrainError("My OpenAI API key was rejected. Please check it.")
        except o.RateLimitError as e:
            if "insufficient_quota" in str(e):
                raise BrainError("Your OpenAI account is out of credit.")
            raise BrainError("I'm being rate limited. Try again in a moment.")
        except o.NotFoundError:
            raise BrainError(f"The model {self.model} isn't available. Check config.yaml.")
        except (o.APITimeoutError, o.APIConnectionError):
            raise BrainError("I can't reach OpenAI right now. Check your internet connection.")
        except o.APIStatusError as e:
            raise BrainError(f"OpenAI returned an error, code {e.status_code}.")


# ================================================================ Gemini

class GeminiBrain(_Brain):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        from google import genai
        from google.genai import errors, types
        if not os.environ.get("GEMINI_API_KEY"):
            raise BrainError("The Gemini API key isn't set.")
        self.types, self.errors = types, errors
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                                   http_options=types.HttpOptions(timeout=int(self.timeout * 1000)))
        self.model = cfg["brain"]["gemini_model"]
        decls = []
        for t in actions.TOOLS:
            kw = {"name": t["name"], "description": t["description"]}
            if t["parameters"]["properties"]:
                kw["parameters_json_schema"] = t["parameters"]
            decls.append(types.FunctionDeclaration(**kw))
        self.tool = types.Tool(function_declarations=decls)

    def _run(self, text, history):
        types = self.types
        config = types.GenerateContentConfig(
            system_instruction=self._system(), tools=[self.tool], temperature=self.temperature,
            max_output_tokens=400,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        turn: list = [types.Content(role="user", parts=[types.Part.from_text(text=text)])]
        try:
            for _ in range(self.max_rounds):
                resp = self.client.models.generate_content(model=self.model, contents=history + turn, config=config)
                if not resp.candidates or not resp.candidates[0].content:
                    raise BrainError("The AI returned an empty answer. Please try again.")
                turn.append(resp.candidates[0].content)  # keeps thought signatures intact
                calls = resp.function_calls or []
                if not calls:
                    return resp.text or "", turn
                parts = [types.Part.from_function_response(name=c.name, response={"result": _tool_call(c.name, c.args)})
                         for c in calls]
                turn.append(types.Content(role="user", parts=parts))
            return "I couldn't finish that in a reasonable number of steps.", turn
        except self.errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 429:
                raise BrainError("I'm being rate limited by Gemini. Try again in a moment.")
            if code in (401, 403):
                raise BrainError("My Gemini API key was rejected. Please check it.")
            if code == 404:
                raise BrainError(f"The model {self.model} isn't available. Check config.yaml.")
            raise BrainError(f"Gemini returned an error, code {code}.")
        except (OSError, TimeoutError) as e:
            raise BrainError("I can't reach Gemini right now. Check your internet connection.") from e


# ================================================================ fake

class FakeBrain:
    """Deterministic test brain. Syntax:  tool_name arg1 "arg 2"   or   tool_name key=value"""

    def reset(self) -> None:
        pass

    def handle(self, text: str) -> str:
        text = text.strip()
        if text.lower() in {"help", "?", "tools"}:
            return self.help()
        try:
            parts = [p.strip("\"'") for p in shlex.split(text, posix=False)]
        except ValueError as e:
            return f"Couldn't parse that: {e}"
        name, raw = parts[0], parts[1:]
        tool = next((t for t in actions.TOOLS if t["name"] == name), None)
        if tool is None:
            return f"Unknown tool {name}. Type help for the list."
        params = list(tool["parameters"]["properties"])
        args: dict = {}
        for i, item in enumerate(raw):
            key, sep, value = item.partition("=")
            if sep and key in params:
                args[key] = value.strip("\"'")
            elif i < len(params):
                args[params[i]] = item
        if "steps" in args:
            args["steps"] = int(args["steps"])
        return actions.run_tool(name, args)

    @staticmethod
    def help() -> str:
        lines = ["Tools (fake brain syntax: tool arg1 \"arg two\"):"]
        for t in actions.TOOLS:
            params = " ".join(t["parameters"]["properties"])
            lines.append(f"  {t['name']} {params}".rstrip())
        return "\n".join(lines)


def make_brain(cfg: dict, fake: bool = False, provider: str | None = None):
    if fake:
        return FakeBrain()
    provider = (provider or cfg["brain"]["provider"]).lower()
    if provider == "openai":
        return OpenAIBrain(cfg)
    if provider == "gemini":
        return GeminiBrain(cfg)
    raise BrainError(f"Unknown brain provider {provider}. Use openai or gemini.")
