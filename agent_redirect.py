"""PreToolUse tip for a subagent launch (Claude Code `Agent`/`Task`): a launch of a search-only subagent for a
targeted question carries the Smart Tool call that answers it, and every other launch (coding, whole-product
reviews, web research, custom or ambiguous types) gets nothing. The launch itself is never denied."""
import re

LAUNCH_TOOLS = frozenset({"Agent", "Task"})
SEARCH_ONLY_TYPES = frozenset({"Explore"})
MAX_PROMPT_CHARS = 1500
BROAD_SCOPE = re.compile(
    r"\b(review|audit|assess|inventory|survey|broad|whole|entire|every|each file|all files|all the|across the "
    r"(?:repo|repository|product|codebase|project)|revis\w*|audit\w*|invent[aá]rio|inteir[oa]|todos os|todas as|"
    r"produto todo|em todo)\b", re.I)
WEB_SCOPE = re.compile(r"https?://|\b(web|internet|online|documenta[cç][aã]o oficial|official docs)\b", re.I)

LAUNCH_TIP = (
    "for a search like this, call "
    "mcp__smart-tool__smart_scout(project_root, question) yourself (project_root is your working directory). It reads "
    "the files inside Smart Tool and returns only the lines that answer, at a fraction of the cost of a subagent.")


def reason(payload):
    """The tip for a launch of a search-only subagent with a short, targeted prompt, or None when the
    launch should run: another tool, another subagent type, a long prompt, a broad-scope or web-research wording."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in LAUNCH_TOOLS:
        return None
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    prompt = str(tool_input.get("prompt") or "")
    if tool_input.get("subagent_type") not in SEARCH_ONLY_TYPES or not prompt.strip():
        return None
    if len(prompt) > MAX_PROMPT_CHARS or BROAD_SCOPE.search(prompt) or WEB_SCOPE.search(prompt):
        return None
    return LAUNCH_TIP
