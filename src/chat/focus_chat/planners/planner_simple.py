import json  # <--- 确保导入 json
import traceback
from typing import List, Dict, Any, Optional
from rich.traceback import install
from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config
from src.chat.focus_chat.info.info_base import InfoBase
from src.chat.focus_chat.info.obs_info import ObsInfo
from src.chat.focus_chat.info.cycle_info import CycleInfo
from src.chat.focus_chat.info.action_info import ActionInfo
from src.chat.focus_chat.info.structured_info import StructuredInfo
from src.chat.focus_chat.info.relation_info import RelationInfo
from src.chat.focus_chat.info.expression_selection_info import ExpressionSelectionInfo
from src.common.logger import get_logger
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.focus_chat.planners.action_manager import ActionManager
from json_repair import repair_json
from src.chat.focus_chat.planners.base_planner import BasePlanner
from datetime import datetime

logger = get_logger("planner")

install(extra_lines=3)


def init_prompt():
    Prompt(
        """
{time_block}
{indentify_block}你现在正在参与以下的聊天，以下是具体的聊天内容：

{chat_content_block}

{self_info_block}
{relation_info_block}

{cycle_info_block}

{moderation_prompt}
注意，除了下面动作选项之外，你在群聊里不能做其他任何事情，这是你能力的边界，现在请你选择合适的action:

{action_options_text}

请以动作的输出要求，以严格的 JSON 格式输出，且仅包含 JSON 内容。
请输出你提取的JSON，不要有任何其他文字或解释：

""",
        "simple_planner_prompt",
    )

    Prompt(
        """
动作：{action_name}
该动作的描述：{action_description}
使用该动作的场景：
{action_require}
输出要求：
{{
    "action": "{action_name}",{action_parameters}
}}
""",
        "action_prompt",
    )


class ActionPlanner(BasePlanner):
    def __init__(self, log_prefix: str, action_manager: ActionManager):
        super().__init__(log_prefix, action_manager)
        # LLM规划器配置
        self.planner_llm = LLMRequest(
            model=global_config.model.planner,
            request_type="focus.planner",  # 用于动作规划
        )

        self.utils_llm = LLMRequest(
            model=global_config.model.utils_small,
            request_type="focus.planner",  # 用于动作规划
        )

    async def plan(
        self, all_plan_info: List[InfoBase], running_memorys: List[Dict[str, Any]], loop_start_time: float
    ) -> Dict[str, Any]:
        """
        规划器 (Planner): 使用LLM根据上下文决定做出什么动作。

        参数:
            all_plan_info: 所有计划信息
            running_memorys: 回忆信息
            loop_start_time: 循环开始时间
        """

        action = "no_reply"  # 默认动作
        reasoning = "规划器初始化默认"
        action_data = {}

        try:
            # 获取观察信息
            extra_info: list[str] = []

            self_info = ""
            cycle_info = ""
            structured_info = ""
            extra_info = []
            observed_messages = []
            observed_messages_str = ""
            chat_type = "group"
            is_group_chat = True
            relation_info = ""
            selected_expressions = []
            for info in all_plan_info:
                if isinstance(info, ObsInfo):
                    observed_messages = info.get_talking_message()
                    observed_messages_str = info.get_talking_message_str_truncate()
                    chat_type = info.get_chat_type()
                    is_group_chat = chat_type == "group"
                elif isinstance(info, CycleInfo):
                    cycle_info = info.get_observe_info()
                elif isinstance(info, RelationInfo):
                    relation_info = info.get_processed_info()
                elif isinstance(info, StructuredInfo):
                    structured_info = info.get_processed_info()
                elif isinstance(info, ExpressionSelectionInfo):
                    selected_expressions = info.get_expressions_for_action_data()
                else:
                    extra_info.append(info.get_processed_info())
                # elif not isinstance(info, ActionInfo):  # 跳过已处理的ActionInfo
                # extra_info.append(info.get_processed_info())

            # 获取经过modify_actions处理后的最终可用动作集
            # 注意：动作的激活判定现在在主循环的modify_actions中完成
            # 使用Focus模式过滤动作
            current_available_actions_dict = self.action_manager.get_using_actions_for_mode("focus")

            # 获取完整的动作信息
            all_registered_actions = self.action_manager.get_registered_actions()
            current_available_actions = {}
            for action_name in current_available_actions_dict.keys():
                if action_name in all_registered_actions:
                    current_available_actions[action_name] = all_registered_actions[action_name]
                else:
                    logger.warning(f"{self.log_prefix}使用中的动作 {action_name} 未在已注册动作中找到")

            # 如果没有可用动作或只有no_reply动作，直接返回no_reply
            if not current_available_actions or (
                len(current_available_actions) == 1 and "no_reply" in current_available_actions
            ):
                action = "no_reply"
                reasoning = "没有可用的动作" if not current_available_actions else "只有no_reply动作可用，跳过规划"
                logger.info(f"{self.log_prefix}{reasoning}")
                self.action_manager.restore_actions()
                logger.debug(
                    f"{self.log_prefix}[focus]沉默后恢复到默认动作集, 当前可用: {list(self.action_manager.get_using_actions().keys())}"
                )
                return {
                    "action_result": {"action_type": action, "action_data": action_data, "reasoning": reasoning},
                    "observed_messages": observed_messages,
                }

            # --- 构建提示词 (调用修改后的 PromptBuilder 方法) ---
            prompt = await self.build_planner_prompt(
                self_info_block=self_info,
                relation_info_block=relation_info,
                is_group_chat=is_group_chat,  # <-- Pass HFC state
                chat_target_info=None,
                observed_messages_str=observed_messages_str,  # <-- Pass local variable
                structured_info=structured_info,  # <-- Pass SubMind info
                current_available_actions=current_available_actions,  # <-- Pass determined actions
                cycle_info=cycle_info,  # <-- Pass cycle info
                extra_info=extra_info,
                running_memorys=running_memorys,
            )

            # --- 调用 LLM (普通文本生成) ---
            llm_content = None
            try:
                prompt = f"{prompt}"
                llm_content, (reasoning_content, _) = await self.planner_llm.generate_response_async(prompt=prompt)

                logger.info(f"{self.log_prefix}规划器原始提示词: {prompt}")
                logger.info(f"{self.log_prefix}规划器原始响应: {llm_content}")
                logger.info(f"{self.log_prefix}规划器推理: {reasoning_content}")

            except Exception as req_e:
                logger.error(f"{self.log_prefix}LLM 请求执行失败: {req_e}")
                reasoning = f"LLM 请求失败，你的模型出现问题: {req_e}"
                action = "no_reply"

            if llm_content:
                try:
                    fixed_json_string = repair_json(llm_content)
                    if isinstance(fixed_json_string, str):
                        try:
                            parsed_json = json.loads(fixed_json_string)
                        except json.JSONDecodeError as decode_error:
                            logger.error(f"JSON解析错误: {str(decode_error)}")
                            parsed_json = {}
                    else:
                        # 如果repair_json直接返回了字典对象，直接使用
                        parsed_json = fixed_json_string

                    # 处理repair_json可能返回列表的情况
                    if isinstance(parsed_json, list):
                        if parsed_json:
                            # 取列表中最后一个元素（通常是最完整的）
                            parsed_json = parsed_json[-1]
                            logger.warning(f"{self.log_prefix}LLM返回了多个JSON对象，使用最后一个: {parsed_json}")
                        else:
                            parsed_json = {}

                    # 确保parsed_json是字典
                    if not isinstance(parsed_json, dict):
                        logger.error(f"{self.log_prefix}解析后的JSON不是字典类型: {type(parsed_json)}")
                        parsed_json = {}

                    # 提取决策，提供默认值
                    extracted_action = parsed_json.get("action", "no_reply")
                    extracted_reasoning = ""

                    # 将所有其他属性添加到action_data
                    action_data = {}
                    for key, value in parsed_json.items():
                        if key not in ["action", "reasoning"]:
                            action_data[key] = value

                    action_data["self_info_block"] = self_info

                    extra_info_block = "\n".join(extra_info)
                    extra_info_block += f"\n{structured_info}"
                    if extra_info or structured_info:
                        extra_info_block = f"以下是一些额外的信息，现在请你阅读以下内容，进行决策\n{extra_info_block}\n以上是一些额外的信息，现在请你阅读以下内容，进行决策"
                    else:
                        extra_info_block = ""

                    action_data["extra_info_block"] = extra_info_block

                    if relation_info:
                        action_data["relation_info_block"] = relation_info

                    # 将选中的表达方式传递给action_data
                    if selected_expressions:
                        action_data["selected_expressions"] = selected_expressions
                        logger.debug(f"{self.log_prefix} 传递{len(selected_expressions)}个选中的表达方式到action_data")

                    action_data["loop_start_time"] = loop_start_time

                    # 对于reply动作不需要额外处理，因为相关字段已经在上面的循环中添加到action_data

                    if extracted_action not in current_available_actions:
                        logger.warning(
                            f"{self.log_prefix}LLM 返回了当前不可用或无效的动作: '{extracted_action}' (可用: {list(current_available_actions.keys())})，将强制使用 'no_reply'"
                        )
                        action = "no_reply"
                        reasoning = f"LLM 返回了当前不可用的动作 '{extracted_action}' (可用: {list(current_available_actions.keys())})。原始理由: {extracted_reasoning}"
                    else:
                        # 动作有效且可用
                        action = extracted_action
                        reasoning = extracted_reasoning

                except Exception as json_e:
                    logger.warning(f"{self.log_prefix}解析LLM响应JSON失败 {json_e}. LLM原始输出: '{llm_content}'")
                    traceback.print_exc()
                    reasoning = f"解析LLM响应JSON失败: {json_e}. 将使用默认动作 'no_reply'."
                    action = "no_reply"

        except Exception as outer_e:
            logger.error(f"{self.log_prefix}Planner 处理过程中发生意外错误，规划失败，将执行 no_reply: {outer_e}")
            traceback.print_exc()
            action = "no_reply"
            reasoning = f"Planner 内部处理错误: {outer_e}"

        # 恢复到默认动作集
        self.action_manager.restore_actions()
        logger.debug(
            f"{self.log_prefix}规划后恢复到默认动作集, 当前可用: {list(self.action_manager.get_using_actions().keys())}"
        )

        action_result = {"action_type": action, "action_data": action_data, "reasoning": reasoning}

        plan_result = {
            "action_result": action_result,
            "observed_messages": observed_messages,
            "action_prompt": prompt,
        }

        return plan_result

    async def build_planner_prompt(
        self,
        self_info_block: str,
        relation_info_block: str,
        is_group_chat: bool,  # Now passed as argument
        chat_target_info: Optional[dict],  # Now passed as argument
        observed_messages_str: str,
        structured_info: Optional[str],
        current_available_actions: Dict[str, ActionInfo],
        cycle_info: Optional[str],
        extra_info: list[str],
        running_memorys: List[Dict[str, Any]],
    ) -> str:
        """构建 Planner LLM 的提示词 (获取模板并填充数据)"""
        try:
            if relation_info_block:
                relation_info_block = f"以下是你和别人的关系描述：\n{relation_info_block}"
            else:
                relation_info_block = ""

            memory_str = ""
            if running_memorys:
                memory_str = "以下是当前在聊天中，你回忆起的记忆：\n"
                for running_memory in running_memorys:
                    memory_str += f"{running_memory['content']}\n"

            chat_context_description = "你现在正在一个群聊中"
            chat_target_name = None  # Only relevant for private
            if not is_group_chat and chat_target_info:
                chat_target_name = (
                    chat_target_info.get("person_name") or chat_target_info.get("user_nickname") or "对方"
                )
                chat_context_description = f"你正在和 {chat_target_name} 私聊"

            chat_content_block = ""
            if observed_messages_str:
                chat_content_block = f"\n{observed_messages_str}"
            else:
                chat_content_block = "你还未开始聊天"

            action_options_block = ""
            for using_actions_name, using_actions_info in current_available_actions.items():
                using_action_prompt = await global_prompt_manager.get_prompt_async("action_prompt")

                if using_actions_info["parameters"]:
                    param_text = "\n"
                    for param_name, param_description in using_actions_info["parameters"].items():
                        param_text += f'    "{param_name}":"{param_description}"\n'
                    param_text = param_text.rstrip("\n")
                else:
                    param_text = ""

                require_text = ""
                for require_item in using_actions_info["require"]:
                    require_text += f"- {require_item}\n"
                require_text = require_text.rstrip("\n")

                using_action_prompt = using_action_prompt.format(
                    action_name=using_actions_name,
                    action_description=using_actions_info["description"],
                    action_parameters=param_text,
                    action_require=require_text,
                )

                action_options_block += using_action_prompt

            extra_info_block = "\n".join(extra_info)
            extra_info_block += f"\n{structured_info}"
            if extra_info or structured_info:
                extra_info_block = f"以下是一些额外的信息，现在请你阅读以下内容，进行决策\n{extra_info_block}\n以上是一些额外的信息，现在请你阅读以下内容，进行决策"
            else:
                extra_info_block = ""

            # moderation_prompt_block = "请不要输出违法违规内容，不要输出色情，暴力，政治相关内容，如有敏感内容，请规避。"
            moderation_prompt_block = ""

            # 获取当前时间
            time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

            bot_name = global_config.bot.nickname
            if global_config.bot.alias_names:
                bot_nickname = f",也有人叫你{','.join(global_config.bot.alias_names)}"
            else:
                bot_nickname = ""
            bot_core_personality = global_config.personality.personality_core
            indentify_block = f"你的名字是{bot_name}{bot_nickname}，你{bot_core_personality}："

            planner_prompt_template = await global_prompt_manager.get_prompt_async("simple_planner_prompt")
            prompt = planner_prompt_template.format(
                relation_info_block=relation_info_block,
                self_info_block=self_info_block,
                time_block=time_block,
                chat_context_description=chat_context_description,
                chat_content_block=chat_content_block,
                cycle_info_block=cycle_info,
                action_options_text=action_options_block,
                moderation_prompt=moderation_prompt_block,
                indentify_block=indentify_block,
            )
            return prompt

        except Exception as e:
            logger.error(f"构建 Planner 提示词时出错: {e}")
            logger.error(traceback.format_exc())
            return "构建 Planner Prompt 时出错"


init_prompt()
