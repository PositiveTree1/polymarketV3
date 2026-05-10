# AGENTS.md

## Python coding rules

- Only UI code in titan_ui.py, everything else must be in the API.
- Use strong typing throughout the codebase.
- Never use hasattr, it is strongly typed
- Add explicit type annotations for function parameters and return values.
- Avoid `Any`; use `Protocol`, `TypeVar`, `TypedDict`, `dataclass`, or Pydantic models where suitable.
- Keep business/domain objects typed rather than passing raw `dict` objects around.
- Use `Path` from `pathlib` rather than string paths where practical.
- Prefer small typed functions over large loosely typed functions.
- If changing signatures, update all callers and tests.
- Only change the minimum and don't touch code that is no directly related to the request