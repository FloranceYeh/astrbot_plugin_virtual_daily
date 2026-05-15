import asyncio
import json
import random
import re
import time
import zoneinfo
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.provider import Provider


@dataclass
class ProactiveDecision:
    should_send: bool
    delay_seconds: int
    target_type: str
    target_id: str
    content: str
    reason: str


class VirtualDailyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.client = None
        self._recent_messages: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=self._cfg_int("context_max_messages", 80))
        )
        self._loop_task: asyncio.Task | None = None
        self._send_tasks: set[asyncio.Task] = set()
        self._last_experience = ""
        self._last_decision: ProactiveDecision | None = None
        self._last_run_at = 0.0

    async def initialize(self):
        if self._cfg_bool("enabled", True):
            self._loop_task = asyncio.create_task(self._run_loop())
            logger.info("VirtualDaily proactive loop started")

    async def terminate(self):
        if self._loop_task:
            self._loop_task.cancel()
        for task in list(self._send_tasks):
            task.cancel()
        await asyncio.gather(
            *([self._loop_task] if self._loop_task else []),
            *self._send_tasks,
            return_exceptions=True,
        )
        self._send_tasks.clear()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def capture_context(self, event: AiocqhttpMessageEvent):
        if not self.client:
            self.client = event.bot
            logger.debug("VirtualDaily AIOCQHTTP client initialized")

        text = (event.message_str or "").strip()
        if not text or text.startswith(tuple(self._cfg_list("ignore_prefixes", ["/"]))):
            return

        sender = event.get_sender_name() or event.get_sender_id()
        session_key = self._event_session_key(event)
        self._recent_messages[session_key].append(f"{sender}: {text}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("虚拟日常状态", alias={"日常状态"})
    async def status(self, event: AiocqhttpMessageEvent):
        decision = self._last_decision
        lines = [
            f"状态: {'启用' if self._cfg_bool('enabled', True) else '关闭'}",
            f"检查间隔: {self._cfg_int('interval_minutes', 60)} 分钟",
            f"延迟区间: {self._delay_bounds()[0]}-{self._delay_bounds()[1]} 秒",
            f"见闻策略: {self._experience_policy()}",
            f"已记录会话: {len(self._recent_messages)} 个",
            f"上次运行: {self._format_ts(self._last_run_at)}",
            f"上次经历: {self._last_experience or '无'}",
        ]
        if decision:
            lines.extend(
                [
                    f"上次决策: {'发送' if decision.should_send else '不发送'}",
                    f"目标: {decision.target_type}:{decision.target_id or '-'}",
                    f"延迟: {decision.delay_seconds} 秒",
                    f"理由: {decision.reason}",
                    f"内容: {decision.content or '-'}",
                ]
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("虚拟日常触发", alias={"触发日常"})
    async def trigger_once(self, event: AiocqhttpMessageEvent):
        topic = event.message_str.partition(" ")[2].strip()
        session_key = self._event_session_key(event)
        target_type, target_id = self._target_from_event(event)
        experience = await self._maybe_generate_experience(topic=topic)
        decision = await self._decide(
            experience=experience,
            session_key=session_key,
            fallback_target_type=target_type,
            fallback_target_id=target_id,
        )
        self._last_experience = experience
        self._last_decision = decision
        self._last_run_at = time.time()

        if not decision.should_send:
            yield event.plain_result(
                f"生成经历:\n{experience or '本轮未提供见闻'}\n\nLLM 决定不发送。\n理由: {decision.reason}"
            )
            return

        await self._send_decision(decision, ignore_delay=True)
        yield event.plain_result(
            f"生成经历:\n{experience or '本轮未提供见闻'}\n\n已按 LLM 决策发送。\n内容:\n{decision.content}"
        )

    async def _run_loop(self):
        while True:
            try:
                await asyncio.sleep(self._next_interval_seconds())
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"VirtualDaily loop failed: {e}")

    async def _run_once(self):
        if not self.client:
            logger.debug("VirtualDaily skipped: platform client is not ready")
            return

        session_key, target_type, target_id = self._pick_target()
        if not target_id:
            logger.warning("VirtualDaily skipped: no target group/user configured or observed")
            return

        experience = await self._maybe_generate_experience()
        decision = await self._decide(
            experience=experience,
            session_key=session_key,
            fallback_target_type=target_type,
            fallback_target_id=target_id,
        )
        self._last_experience = experience
        self._last_decision = decision
        self._last_run_at = time.time()

        if not decision.should_send:
            logger.info(f"VirtualDaily decided not to send: {decision.reason}")
            return

        task = asyncio.create_task(self._send_decision(decision))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    async def _generate_experience(self, topic: str = "") -> str:
        provider = self._get_provider(self._cfg_str("experience_provider_id", ""))
        prompt = self._cfg_str(
            "experience_prompt",
            "请为一个虚拟角色生成一段刚刚发生的日常见闻或经历，80字以内，具体、自然，不要解释。",
        )
        if topic:
            prompt += f"\n额外主题: {topic}"

        if isinstance(provider, Provider):
            try:
                response = await provider.text_chat(prompt=prompt)
                text = self._clean_text(response.completion_text)
                if text:
                    return text
            except Exception as e:
                logger.error(f"VirtualDaily experience LLM failed: {e}")

        return self._fallback_experience(topic)

    async def _maybe_generate_experience(self, topic: str = "") -> str:
        if not self._should_include_experience():
            return ""
        return await self._generate_experience(topic=topic)

    async def _decide(
        self,
        *,
        experience: str,
        session_key: str,
        fallback_target_type: str,
        fallback_target_id: str,
    ) -> ProactiveDecision:
        provider = self._get_provider(self._cfg_str("decision_provider_id", ""))
        if not isinstance(provider, Provider):
            return ProactiveDecision(False, 0, fallback_target_type, fallback_target_id, "", "未配置可用 LLM")

        contexts = list(self._recent_messages.get(session_key, []))
        target_hint = {
            "target_type": fallback_target_type,
            "target_id": fallback_target_id,
        }
        system_prompt = self._cfg_str(
            "decision_prompt",
            (
                "你负责决定机器人是否应该主动发言。只输出 JSON，不要 Markdown。"
                "字段: should_send(bool), delay_seconds(int), target_type('group'或'user'), "
                "target_id(string), content(string), reason(string)。"
                "主动消息必须自然、简短、像顺手分享，不要提到你在模拟经历或读取上下文。"
            ),
        )
        user_prompt = json.dumps(
            {
                "experience": experience,
                "experience_policy": self._experience_policy(),
                "instruction": (
                    "如果 experience 为空，不要编造见闻，只自然地询问用户近况。"
                    "如果 experience 不为空，可以按聊天氛围选择分享见闻或询问近况。"
                ),
                "recent_context": contexts[-self._cfg_int("context_max_messages", 80) :],
                "fallback_target": target_hint,
                "min_delay_seconds": self._delay_bounds()[0],
                "max_delay_seconds": self._cfg_int("max_delay_seconds", 1800),
                "content_max_chars": self._cfg_int("content_max_chars", 120),
            },
            ensure_ascii=False,
        )

        try:
            response = await provider.text_chat(system_prompt=system_prompt, prompt=user_prompt)
            data = self._extract_json(response.completion_text)
            return self._parse_decision(data, fallback_target_type, fallback_target_id)
        except Exception as e:
            logger.error(f"VirtualDaily decision LLM failed: {e}")
            return ProactiveDecision(False, 0, fallback_target_type, fallback_target_id, "", str(e))

    async def _send_decision(self, decision: ProactiveDecision, *, ignore_delay: bool = False):
        if not decision.content.strip():
            return
        if not ignore_delay and decision.delay_seconds > 0:
            await asyncio.sleep(decision.delay_seconds)

        if not self.client:
            logger.error("VirtualDaily cannot send: platform client is not ready")
            return

        if decision.target_type == "user":
            await self.client.send_private_msg(
                user_id=int(decision.target_id),
                message=[{"type": "text", "data": {"text": decision.content}}],
            )
            return

        await self.client.send_group_msg(
            group_id=int(decision.target_id),
            message=[{"type": "text", "data": {"text": decision.content}}],
        )

    def _parse_decision(
        self, data: dict[str, Any], fallback_target_type: str, fallback_target_id: str
    ) -> ProactiveDecision:
        min_delay, max_delay = self._delay_bounds()
        max_chars = self._cfg_int("content_max_chars", 120)
        target_type = str(data.get("target_type") or fallback_target_type).lower()
        if target_type not in {"group", "user"}:
            target_type = fallback_target_type
        target_id = re.sub(r"\D", "", str(data.get("target_id") or fallback_target_id))
        content = self._clean_text(str(data.get("content") or ""))[:max_chars]
        delay = self._clamp_int(data.get("delay_seconds", min_delay), min_delay, max_delay)
        should_send = self._as_bool(data.get("should_send")) and bool(target_id) and bool(content)
        return ProactiveDecision(
            should_send=should_send,
            delay_seconds=delay,
            target_type=target_type,
            target_id=target_id,
            content=content,
            reason=self._clean_text(str(data.get("reason") or "")),
        )

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                raise
            return json.loads(match.group(0))

    def _get_provider(self, provider_id: str):
        return self.context.get_provider_by_id(provider_id) or self.context.get_using_provider()

    def _pick_target(self) -> tuple[str, str, str]:
        groups = [str(x) for x in self._cfg_list("target_groups", []) if str(x).isdigit()]
        users = [str(x) for x in self._cfg_list("target_users", []) if str(x).isdigit()]
        candidates: list[tuple[str, str, str]] = []
        candidates.extend((f"group:{gid}", "group", gid) for gid in groups)
        candidates.extend((f"user:{uid}", "user", uid) for uid in users)

        if self._cfg_bool("allow_recent_sessions", True):
            for key in self._recent_messages:
                target_type, target_id = key.split(":", 1)
                candidates.append((key, target_type, target_id))

        return random.choice(candidates) if candidates else ("", "group", "")

    @staticmethod
    def _event_session_key(event: AiocqhttpMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"user:{event.get_sender_id()}"

    @staticmethod
    def _target_from_event(event: AiocqhttpMessageEvent) -> tuple[str, str]:
        group_id = event.get_group_id()
        if group_id:
            return "group", str(group_id)
        return "user", str(event.get_sender_id())

    def _next_interval_seconds(self) -> int:
        base = max(1, self._cfg_int("interval_minutes", 60)) * 60
        jitter = max(0, self._cfg_int("interval_jitter_seconds", 600))
        return max(1, base + random.randint(-jitter, jitter))

    def _delay_bounds(self) -> tuple[int, int]:
        min_delay = max(0, self._cfg_int("min_delay_seconds", 0))
        max_delay = max(0, self._cfg_int("max_delay_seconds", 1800))
        if min_delay > max_delay:
            return max_delay, min_delay
        return min_delay, max_delay

    def _experience_policy(self) -> str:
        policy = self._cfg_str("experience_send_policy", "always").strip().lower()
        if policy in {"never", "none", "off", "ask_only"}:
            return "never"
        if policy in {"probability", "probabilistic", "random", "chance"}:
            return "probability"
        return "always"

    def _should_include_experience(self) -> bool:
        policy = self._experience_policy()
        if policy == "never":
            return False
        if policy == "probability":
            chance = self._clamp_int(self.config.get("experience_send_probability", 50), 0, 100)
            return random.randint(1, 100) <= chance
        return True

    def _fallback_experience(self, topic: str = "") -> str:
        places = ["便利店", "路口", "窗边", "电梯里", "楼下", "书桌前"]
        actions = [
            "听见有人小声争论要不要买最后一盒点心",
            "看到云缝里漏下一小块很亮的光",
            "发现杯子里的冰化得比想象中快",
            "等消息时忽然想起一个没讲完的话题",
            "路过时闻到刚烤好的面包味",
        ]
        suffix = f"，又想到{topic}" if topic else ""
        return f"刚才在{random.choice(places)}，{random.choice(actions)}{suffix}。"

    @staticmethod
    def _clean_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().strip('"').strip("'")

    @staticmethod
    def _clamp_int(value: Any, min_value: int, max_value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = min_value
        return max(min_value, min(max_value, parsed))

    def _cfg_str(self, key: str, default: str) -> str:
        return str(self.config.get(key, default) or default)

    def _cfg_int(self, key: str, default: int) -> int:
        return self._clamp_int(self.config.get(key, default), -2**31, 2**31 - 1)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return self._as_bool(self.config.get(key, default), default)

    def _cfg_list(self, key: str, default: list[Any]) -> list[Any]:
        value = self.config.get(key, default)
        return value if isinstance(value, list) else default

    def _format_ts(self, ts: float) -> str:
        if not ts:
            return "无"
        timezone_name = self.context.get_config().get("timezone") or "Asia/Shanghai"
        tz = zoneinfo.ZoneInfo(timezone_name)
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", ""}:
                return False
        return default
