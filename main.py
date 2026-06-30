import argparse
import asyncio
import sys

from app.agent.manus import Manus
from app.logger import logger


def _format_candidate(recipe) -> str:
    """把提炼出的候选经验渲染成终端可读的预览。"""
    lines = ["\n" + "=" * 60, "📝 本次任务已成功，提炼出以下经验（确认后存入经验库）：", f"任务：{recipe.task}"]
    if recipe.steps:
        lines.append("步骤：")
        lines.extend(f"  {s}" for s in recipe.steps)
    if recipe.result_summary:
        lines.append(f"结果：{recipe.result_summary}")
    if recipe.tips:
        lines.append("关键提示：")
        lines.extend(f"  - {t}" for t in recipe.tips)
    lines.append("=" * 60)
    return "\n".join(lines)


async def _maybe_save_experience(agent: Manus) -> None:
    """跑完交互确认入口（Entry A）。

    仅在经验库启用、且 terminate(status=success) 时触发；此刻 agent.memory/llm
    仍可用（cleanup 只关浏览器/MCP，不动 memory）。非交互运行（stdin 非 TTY，
    如 --prompt 脚本）跳过保存。想修改经验：事后手编 experience/recipes.jsonl。
    全程 best-effort，绝不影响主流程。
    """
    try:
        from app.experience import distill_recipe, is_enabled, store

        if not is_enabled():
            return
        if getattr(agent, "_last_terminate_status", None) != "success":
            return

        candidate = await distill_recipe(agent)
        if candidate is None:
            return

        if not sys.stdin.isatty():
            logger.info("非交互模式，跳过经验保存（如需保存请在交互式终端运行）。")
            return

        print(_format_candidate(candidate))
        answer = input("保存这条经验吗？[y/N]: ").strip().lower()
        if answer == "y":
            await store.add(candidate)
            logger.info("✅ 经验已保存。想修改/删除可手编 experience/recipes.jsonl")
        else:
            logger.info("已丢弃该经验。")
    except Exception as e:
        logger.warning(f"经验保存流程出错（忽略）：{e}")


async def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="运行 Manus agent")
    parser.add_argument(
        "--prompt", type=str, required=False, help="输入给 agent 的提示"
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        required=False,
        help="agent 终止前的最大步数（覆盖默认值，复杂任务可调大）",
    )
    args = parser.parse_args()

    # 创建并初始化 Manus agent（--max-steps 给定时覆盖默认值）
    create_kwargs = {}
    if args.max_steps is not None:
        if args.max_steps <= 0:
            logger.warning("--max-steps 必须为正整数，已忽略。")
        else:
            create_kwargs["max_steps"] = args.max_steps
    agent = await Manus.create(**create_kwargs)
    try:
        # 如果提供了命令行提示，则使用它；否则询问用户输入
        prompt = args.prompt if args.prompt else input("请输入你的提示: ")
        if not prompt.strip():
            logger.warning("提供的提示为空。")
            return

        logger.warning("正在处理你的请求...")
        await agent.run(prompt)
        logger.info("请求处理完成。")

        # 任务成功后，交互确认是否把本次流程沉淀进经验库（RAG few-shot）。
        await _maybe_save_experience(agent)
    except KeyboardInterrupt:
        logger.warning("操作被中断。")
    finally:
        # 确保在退出前清理 agent 资源
        await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
