# Language & runtime

- Python >= **3.14**

- US English everywhere (identifiers, comments, errors, logs).

## Project Structure

Always use standard Python library project structure including fully fleshed out `__init__.py`, `__version__.py`, and source files under `src/<project_name>`.
Follow standard pytest project folder structure for testing.

## Formatting & style

- Keep code formatted with `black` (default settings). **CRITICAL:** Before completing any code changes or responding to the user that a fix is complete, you MUST run `poetry run black .` and `poetry run ruff check .`. If Ruff reports issues, you MUST fix them (for example, run `poetry run ruff check . --fix` when appropriate) and re-run `poetry run ruff check .` until all checks pass.
- Use US English for all documentation and comments.
- Include type hints for every variable, parameter, and return value.
- When using Pydantic, always use Pydantic v2.

## .env

Keep `env_template.txt` with any env vars and include helpful comments about default values and uses.
Ensure reading of any env vars always has helpful defaults except in the case of true secrets.

## File Naming convention

Name new files based on the primary class or feature contained therein. Avoid generic names.
Example: if the class is `AIMetricsProviderBase`, the file should be named `ai_metrics_provider_base.py`.

## Security

Never use `random` to generate a random number. Use `secrets`.

## Variables

- Use descriptive names prefixed by type for non-primitive objects (e.g., `dict_settings`, `list_dict_teams`, `random_index`).
- Primitive objects do not need prefixes, except for strings, which should have a `str_` prefix.
- Instances of classes should have a helpful prefix that indicates the class name.
- Do not abbreviate words and never use one-letter variable names. Exceptions include widely used abbreviations such as `config` for configuration, `env` for environment, `idx` for index, `id` for identifier.

## Type hints

- **Everything is typed**: parameters, return values, and **all local variables**.

- **PEP 604 unions** (`str | None`) — never `Optional[str]`.

- Prefer concrete, precise types; use `Any` only at **external boundaries** (e.g., boto3 payloads).

## Typed containers & generics

- Use **built-in generics**: `list[str]`, `dict[str, Any]`, `set[str]`, `tuple[str, int]`.

  Rationale: modern, concise, and the standard in Python 3.11+.

- **Never** use legacy `typing.List`, `typing.Dict`, `typing.Set`, `typing.Tuple`.

  Rationale: deprecated style; hurts readability.

- For **fixed-length tuples** use `tuple[T1, T2]`; for homogeneous variable-length use `tuple[T, ...]`.

  Rationale: communicates shape vs. sequence.

- When a function only **reads** a collection, accept `Sequence[T]` / `Mapping[K, V]`.
- Accept `MutableSequence[T]` / `MutableMapping[K, V]` only when **mutation** is required.

- Import ABCs from **`collections.abc`** (e.g., `from collections.abc import Iterable, Sequence, Mapping, Callable`).

  Rationale: canonical runtime generics in modern Python.

- Callable types are written as `Callable[[ArgType1, ArgType2], ReturnType]`.

  Rationale: explicit signatures aid IDEs and code review.

- For structured dicts with a fixed schema, use **Pydantic V2** models to validate and coerce data.

## Class and object property or method access

Always access class fields and properties directly by name. Do not use `getattr` or `hasattr` — they bypass type checking and signal incomplete knowledge of the class contract. Use direct attribute access and let type checkers catch mistakes at development time.

## Structure & readability

- Move **slowly**, step-by-step; confirm the plan **before** edits.

- No nested functions; flat, testable helpers.

- Assign to a named variable before returning (helps devs with debugging); avoid dense inline returns.

- Keep module-level side effects minimal (no work at import time beyond constants).

## Naming conventions

- Do **not** rename existing variables unless required to complete the task, such as when the purpose of the variable is materially changed.

- Names must help clarify the type or role of the variable in context (e.g., `list_subnet_ids`, `map_headers`, `set_arns`, `dict_user_properties`).
  Rationale: improves readability and reduces dev errors.

## 3rd-party APIs

Always wrap 3rd-party API calls with a factory pattern:

1. Create an ABC for the type of API provider.
2. Subclass to create a wrapper for the specific 3rd-party API.
3. Use a `factory.create_client()` method to instantiate the specific subclass based on a registry (e.g., `.env`).

For 3rd-party API calls, always attempt to discern retryable from non-retryable errors. Create generic methods within the API wrapper class to test for retryable errors (e.g., keep a list of specific exceptions and refer back to this list) and implement retry-with-backoff functionality.

## Logging, errors, and retries

- Wrap any critical calls with `try` to catch all relevant exceptions. Ensure exception objects are caught and logged.
- Use structured, concise log lines; **no timestamps** in messages.
- Prefer `%s` parameterized logging (lazy formatting) over f-strings in log calls.
- Error messages must be actionable; wrap low-level exceptions with context.
- Never just raise an error without an appropriate-level log message.
- Forbid secrets/PII in log messages.
- Do not give up on errors without first deciding whether they are retryable.

## Comment and documentation rules

- **CRITICAL:** Every single function MUST include a comprehensive docstring describing:
  - What the function does (its purpose).
  - Each input parameter's use and type constraints.
  - The exact output structure and any potential return variations.
- Add comments to class headers, new logical blocks, loops, and any place where an important decision occurs.
- When using third-party APIs, provide a helpful API documentation URL in the comments.
- Use only ASCII characters in comments and documentation; avoid special or "fancy" Unicode characters that may render poorly in editors.
- Never remove existing comments unless they are no longer relevant.
- Reword or expand comments for clarity.
- US English only.

## Module import practices

- Enforce import ordering (standard library → third-party → local) with `isort` in pre-commit.
- **Do not** purge modules from `sys.modules`; avoid patterns such as:

  ```python
  import sys
  sys.modules.pop("yaml", None)
  ```

## HTTP API Resiliency

- Implement **bounded exponential backoff** on known transient API endpoint errors.

- Validate inputs **before** calling APIs (fail fast with 4xx-style errors when appropriate).

## Output & change management

- **Never provide diffs** unless specifically requested. Reason: diffs are nearly impossible to copy/paste into an IDE.

- Always deliver **full code blocks** (drop-in, production-ready) at the function or class level unless a surgical edit is requested.

- Keep comments focused on **why**, not restating code.

## Library and config management

The project uses Poetry. Ensure you maintain a clean `pyproject.toml` representation.

## Production Code ONLY

Unless specifically requested for pseudocode — **NEVER** provide examples with incomplete code or assumptions based on hallucinated system features. Only produce full production-ready code.

## Versioning

- Bump the version number whenever external input changes or output behavior changes — even subtle tweaks.
- After a bump, update `README.md`, `pyproject.toml`, and `__version__.py`.

## Git commit message requirements

Begin each commit message with the new version tag for context, e.g., `v1.2.0 updates foo for bar.`

## Branch naming

- If a JIRA ticket or GitHub issue is referenced, prefix the branch name with that identifier.
