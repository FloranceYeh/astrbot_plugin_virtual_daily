import asyncio
import json
import os
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

try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        TextPart,
    )
except ImportError:
    AssistantMessageSegment = None
    TextPart = None


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
        self._session_umos: dict[str, str] = {}
        self._loop_task: asyncio.Task | None = None
        self._send_tasks: set[asyncio.Task] = set()
        self._last_experience = ""
        self._last_decision: ProactiveDecision | None = None
        self._last_run_at = 0.0
        self._trigger_times: list[float] = []
        self._unanswered_counts: dict[str, int] = defaultdict(int)

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

        if self._is_self_message(event):
            return

        text = (event.message_str or "").strip()
        if not text or text.startswith(tuple(self._cfg_list("ignore_prefixes", ["/"]))):
            return

        sender = event.get_sender_name() or event.get_sender_id()
        session_key = self._event_session_key(event)
        self._session_umos[session_key] = event.unified_msg_origin
        self._recent_messages[session_key].append(f"{sender}: {text}")
        if self._unanswered_counts.get(session_key, 0) > 0:
            self._unanswered_counts[session_key] = 0
            logger.info(f"VirtualDaily reply observed, unanswered count reset: {session_key}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("虚拟日常状态", alias={"日常状态"})
    async def status(self, event: AiocqhttpMessageEvent):
        decision = self._last_decision
        lines = [
            f"状态: {'启用' if self._cfg_bool('enabled', True) else '关闭'}",
            f"检查间隔: {self._cfg_int('interval_minutes', 60)} 分钟",
            f"延迟区间: {self._delay_bounds()[0]}-{self._delay_bounds()[1]} 秒",
            f"见闻策略: {self._experience_policy()}",
            "人格来源: 用户配置文档",
            f"消息分割: {'启用' if self._cfg_bool('split_messages_enabled', False) else '关闭'}",
            f"已记录会话: {len(self._recent_messages)} 个",
            f"上次运行: {self._format_ts(self._last_run_at)}",
            f"触发次数: {len(self._trigger_times)}",
            f"触发时间: {self._format_trigger_times()}",
            f"未回应计数: {self._format_unanswered_counts()}",
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
        self._record_trigger("manual", session_key)
        self._session_umos[session_key] = event.unified_msg_origin
        experience = await self._maybe_generate_experience(
            topic=topic,
            unified_msg_origin=event.unified_msg_origin,
            session_key=session_key,
        )
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

        self._record_trigger("scheduled", session_key)
        experience = await self._maybe_generate_experience(
            unified_msg_origin=self._session_umos.get(session_key),
            session_key=session_key,
        )
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

    async def _generate_experience(
        self,
        topic: str = "",
        unified_msg_origin: str | None = None,
        session_key: str = "",
    ) -> str:
        provider = self._get_provider(self._cfg_str("experience_provider_id", ""))
        base_prompt = self._cfg_str(
            "experience_prompt",
            "请以当前角色为主体生成一段刚刚发生的日常见闻或经历，80字以内，具体、自然，不要解释。",
        )
        time_info = self._build_time_info()
        persona = await self._load_persona_document("experience")
        recent_context = list(self._recent_messages.get(session_key, []))

        system_prompt = (
            f"{base_prompt}\n\n"
            "必须严格遵守时间关系：内容要贴合 current_time；可以写当下正在发生的事，"
            "也可以写过去某个明确时间点发生的事，但过去事件不能被写成未来或当下。"
            "如果提到今天、刚才、昨晚、周末、早上、傍晚等相对时间，必须与 current_time、date、weekday、hour 保持一致。"
            "不要编造与当前时段明显冲突的场景。"
        )
        if persona:
            system_prompt = (
                f"{system_prompt}\n\n"
                "当前角色人格文档如下。生成见闻时必须符合角色的人格、语气、生活习惯和设定；"
                "不要复述人格文档，也不要说明你参考了文档。\n"
                f"{persona}"
            )
        if recent_context:
            system_prompt += "\n\n近期聊天上下文可作为氛围参考，但不要机械复述。"

        prompt_payload = {
            "topic": topic,
            "time": time_info,
            "recent_context": recent_context[-self._cfg_int("context_max_messages", 80) :],
            "output": "只输出一段虚拟日常内容，不要解释，不要 Markdown。",
        }
        prompt = json.dumps(prompt_payload, ensure_ascii=False)

        if isinstance(provider, Provider):
            try:
                response = await provider.text_chat(system_prompt=system_prompt, prompt=prompt)
                text = self._clean_text(response.completion_text)
                if text:
                    return text
            except Exception as e:
                logger.error(f"VirtualDaily experience LLM failed: {e}")

        return self._fallback_experience(topic)

    async def _maybe_generate_experience(
        self,
        topic: str = "",
        unified_msg_origin: str | None = None,
        session_key: str = "",
    ) -> str:
        if not self._should_include_experience():
            return ""
        return await self._generate_experience(
            topic=topic, unified_msg_origin=unified_msg_origin, session_key=session_key
        )

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
        persona = await self._load_persona_document("decision")
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
                "发送内容必须符合当前角色人格、近期聊天上下文和严格时间关系。"
            ),
        )
        if persona:
            system_prompt = (
                f"{system_prompt}\n\n"
                "当前角色人格文档如下。决策和发送内容都必须符合该人格、语气、生活习惯和设定；"
                "不要复述人格文档，也不要说明你参考了文档。\n"
                f"{persona}"
            )
        user_prompt = json.dumps(
            {
                "experience": experience,
                "experience_policy": self._experience_policy(),
                "time": self._build_time_info(),
                "instruction": (
                    "如果 experience 为空，不要编造见闻，只自然地询问用户近况。"
                    "如果 experience 不为空，可以按聊天氛围选择分享见闻或询问近况。"
                    "必须严格遵守 time 里的当前时间。可以提到当下，也可以提到过去某个时间点，"
                    "但不能把过去说成未来，不能把早晨、夜晚、昨天、今天等相对时间用错。"
                    "如果 unanswered_proactive_count 较高，说明前几次主动消息没有被回应，"
                    "可以适当降低热情、表现得更克制或轻微失落，但不要责备用户。"
                ),
                "recent_context": contexts[-self._cfg_int("context_max_messages", 80) :],
                "unanswered_proactive_count": self._unanswered_counts.get(session_key, 0),
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
            for part in self._split_message(decision.content):
                await self.client.send_private_msg(
                    user_id=int(decision.target_id),
                    message=[{"type": "text", "data": {"text": part}}],
                )
                await self._sleep_between_split_messages()
            await self._record_proactive_sent(decision)
            return

        for part in self._split_message(decision.content):
            await self.client.send_group_msg(
                group_id=int(decision.target_id),
                message=[{"type": "text", "data": {"text": part}}],
            )
            await self._sleep_between_split_messages()
        await self._record_proactive_sent(decision)

    async def _record_proactive_sent(self, decision: ProactiveDecision):
        content = decision.content.strip()
        if not content:
            return

        session_key = f"{decision.target_type}:{decision.target_id}"
        if self._cfg_bool("add_sent_message_to_context", True):
            self._recent_messages[session_key].append(f"机器人: {content}")
        await self._append_to_astrbot_context(session_key, content)
        self._unanswered_counts[session_key] += 1
        logger.info(
            "VirtualDaily proactive message sent, unanswered count "
            f"{self._unanswered_counts[session_key]}: {session_key}"
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

    async def _load_persona_document(self, usage: str) -> str:
        usage = "experience" if usage == "experience" else "decision"
        inline_persona = self._cfg_str(f"{usage}_persona_document", "").strip()
        path = self._cfg_str(f"{usage}_persona_document_path", "").strip()
        if not inline_persona and not path:
            inline_persona = self._cfg_str("persona_document", "").strip()
            path = self._cfg_str("persona_document_path", "").strip()
        text = inline_persona

        if path:
            try:
                if not os.path.isabs(path):
                    path = os.path.join(os.path.dirname(__file__), path)
                with open(path, "r", encoding="utf-8") as f:
                    file_text = f.read().strip()
                if file_text:
                    text = file_text
            except OSError as e:
                logger.warning(f"VirtualDaily failed to read persona document: {e}")

        return self._limit_persona_document(text)

    async def _append_to_astrbot_context(self, session_key: str, content: str):
        if not self._cfg_bool("add_sent_message_to_astrbot_context", True):
            return

        unified_msg_origin = self._session_umos.get(session_key)
        conversation_manager = getattr(self.context, "conversation_manager", None)
        if not unified_msg_origin or not conversation_manager:
            return

        try:
            curr_cid = await conversation_manager.get_curr_conversation_id(
                unified_msg_origin
            )
            if not curr_cid:
                return

            add_message_pair = getattr(conversation_manager, "add_message_pair", None)
            if add_message_pair and AssistantMessageSegment and TextPart:
                assistant_message = AssistantMessageSegment(
                    content=[TextPart(text=content)],
                )
                result = add_message_pair(
                    cid=curr_cid,
                    user_message=None,
                    assistant_message=assistant_message,
                )
                if hasattr(result, "__await__"):
                    await result
                logger.debug(
                    f"VirtualDaily appended proactive message to AstrBot context: {session_key}"
                )
                return
            logger.warning(
                "VirtualDaily could not append proactive message to AstrBot context: "
                "add_message_pair or message segment classes are unavailable"
            )
        except Exception as e:
            logger.warning(f"VirtualDaily failed to append AstrBot context: {e}")

    def _limit_persona_document(self, text: str) -> str:
        text = text.strip()
        max_chars = max(0, self._cfg_int("persona_document_max_chars", 4000))
        if max_chars and len(text) > max_chars:
            return text[:max_chars]
        return text

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

    def _record_trigger(self, trigger_type: str, session_key: str):
        now = time.time()
        self._last_run_at = now
        self._trigger_times.append(now)
        max_records = max(0, self._cfg_int("trigger_time_history_limit", 20))
        if max_records and len(self._trigger_times) > max_records:
            self._trigger_times = self._trigger_times[-max_records:]
        logger.info(
            f"VirtualDaily triggered ({trigger_type}) at {self._format_ts(now)}: {session_key}"
        )

    def _is_self_message(self, event: AiocqhttpMessageEvent) -> bool:
        sender_id = str(event.get_sender_id() or "")
        if not sender_id:
            return False

        candidates = [
            getattr(event, "self_id", None),
            getattr(getattr(event, "message_obj", None), "self_id", None),
            getattr(self.client, "self_id", None),
            getattr(self.client, "uin", None),
        ]
        return any(str(candidate) == sender_id for candidate in candidates if candidate)

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

    def _split_message(self, content: str) -> list[str]:
        text = content.strip()
        if not text:
            return []
        if not self._cfg_bool("split_messages_enabled", False):
            return [text]

        max_chars = max(1, self._cfg_int("split_message_max_chars", 80))
        regex = self._cfg_str("split_message_regex", "").strip()
        split_words = self._cfg_list("split_message_words", ["\n", "。", "！", "？", "，"])
        parts: list[str] = []
        remaining = text
        while len(remaining) > max_chars:
            cut = self._find_split_index(remaining, max_chars, split_words, regex)
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            parts.append(remaining)
        return [part for part in parts if part]

    @staticmethod
    def _find_split_index(
        text: str, max_chars: int, separators: list[Any], regex: str = ""
    ) -> int:
        window = text[: max_chars + 1]
        if regex:
            try:
                matches = list(re.finditer(regex, window))
            except re.error as e:
                logger.warning(f"VirtualDaily split regex is invalid: {e}")
                matches = []
            if matches:
                match = matches[-1]
                cut = match.end() if match.end() > match.start() else match.start()
                if cut > 0:
                    return cut

        best = -1
        for separator in separators:
            sep = str(separator)
            if not sep:
                continue
            index = window.rfind(sep)
            if index > best:
                best = index + len(sep)
        return best if best > 0 else max_chars

    async def _sleep_between_split_messages(self):
        delay = max(0, self._cfg_int("split_message_interval_ms", 800))
        if delay:
            await asyncio.sleep(delay / 1000)

    def _fallback_experience(self, topic: str = "") -> str:
        hour = self._current_datetime().hour
        if 5 <= hour < 11:
            places = ["早餐店门口", "窗边", "路口", "电梯里", "书桌前"]
            actions = [
                "看见晨光落在杯沿上",
                "闻到刚出炉的面包味",
                "听见有人讨论今天的安排",
            ]
        elif 11 <= hour < 14:
            places = ["楼下", "便利店", "路口", "餐桌旁", "窗边"]
            actions = [
                "听见有人纠结午饭吃什么",
                "看到阳光把影子压得很短",
                "发现杯子里的冰化得比想象中快",
            ]
        elif 14 <= hour < 18:
            places = ["书桌前", "窗边", "楼下", "电梯里", "路口"]
            actions = [
                "看到云缝里漏下一小块很亮的光",
                "等消息时忽然想起一个没讲完的话题",
                "听见远处有人把下午茶说得很认真",
            ]
        elif 18 <= hour < 23:
            places = ["楼下", "路口", "便利店", "窗边", "电梯里"]
            actions = [
                "路过时闻到刚烤好的面包味",
                "看见晚灯一盏盏亮起来",
                "听见有人小声争论要不要买最后一盒点心",
            ]
        else:
            places = ["窗边", "书桌前", "便利店", "楼下", "电梯里"]
            actions = [
                "发现夜里声音变得很轻",
                "看到路灯下的影子被拉得很长",
                "等消息时忽然想起一个没讲完的话题",
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
        tz = self._timezone()
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")

    def _current_datetime(self) -> datetime:
        return datetime.now(self._timezone())

    def _timezone(self):
        timezone_name = self.context.get_config().get("timezone") or "Asia/Shanghai"
        return zoneinfo.ZoneInfo(timezone_name)

    def _build_time_info(self) -> dict[str, Any]:
        now = self._current_datetime()
        return {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "weekday": self._weekday_name(now),
            "hour": now.hour,
            "time_of_day": self._time_of_day(now.hour),
            "timezone": str(now.tzinfo),
        }

    @staticmethod
    def _weekday_name(dt: datetime) -> str:
        return ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][
            dt.weekday()
        ]

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 11:
            return "早上"
        if 11 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 23:
            return "晚上"
        return "深夜"

    def _format_trigger_times(self) -> str:
        if not self._trigger_times:
            return "无"
        return "、".join(self._format_ts(ts) for ts in self._trigger_times)

    def _format_unanswered_counts(self) -> str:
        active_counts = {
            key: count for key, count in self._unanswered_counts.items() if count > 0
        }
        if not active_counts:
            return "无"
        return "，".join(f"{key}={count}" for key, count in sorted(active_counts.items()))

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
