import re

TOKEN = re.compile(r"\{\{([^}]+)\}\}")

def match_rule(rule: dict, event: dict) -> bool:
  if not rule["enabled"]:
    return False
  if rule["match_source"] is not None and rule["match_source"] != event["source"]:
    return False
  if rule["match_type"] is not None and rule["match_type"] != event["type"]:
    return False
  return True

def resolve_path(ctx: dict, path: str):
  cur = ctx
  for p in path.split("."):
    if isinstance(cur, dict) and p in cur:
      cur = cur[p]
    else:
      return None
  return cur

def render_template(value, ctx: dict):
  if isinstance(value, dict):
    return {k: render_template(v, ctx) for k, v in value.items()}
  if isinstance(value, list):
    return [render_template(v, ctx) for v in value]
  if isinstance(value, str):
    def repl(m):
      v = resolve_path(ctx, m.group(1).strip())
      return "" if v is None else str(v)
    return TOKEN.sub(repl, value)
  return value
