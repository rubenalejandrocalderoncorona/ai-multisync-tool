"""Public-interface extraction. A "public symbol" is something another piece of code, another repo or a reader of the documentation
can depend on: an exported function or type, an HTTP route, an RPC procedure, a schema model, an environment variable or config key.
Internals (a private function, a log message, a variable name) are NOT symbols.

The router uses this to decide how much a change is worth spending on; the FactStore uses it to know which documents mention which
symbols. Lexical, language-agnostic, deliberately approximate: it errs towards finding too many symbols, never too few.
"""
from __future__ import annotations

import re

_FILE_HEADER = re.compile(r"(?m)^### FILE: (.+)$")


def split_snapshot(snapshot: str | None) -> list[dict]:
    """Split a code snapshot ("### FILE: path\\n...") into {path, text} blocks. A bare string is one anonymous block."""
    text = snapshot or ""
    marks = list(_FILE_HEADER.finditer(text))
    if not marks:
        return [{"path": "", "text": text}] if text else []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append({"path": m.group(1).strip(), "text": text[m.end():end]})
    return out


def _norm(sig) -> str:
    return re.sub(r"\s+", " ", str(sig or "")).strip()


def _add(out: list, kind: str, name: str, sig: str) -> None:
    if name:
        out.append({"kind": kind, "name": name, "sig": _norm(sig)})


def _block_of(text: str, index: int) -> str:
    """The body of a block starting at `index`, up to its closing brace (bounded): schema models change when a field does."""
    open_ = text.find("{", index)
    if open_ < 0:
        return text[index:index + 120]
    depth = 0
    for i in range(open_, min(len(text), open_ + 6000)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[index:i + 1]
    return text[index:open_ + 400]


# ── Java (Spring MVC / JAX-RS / JPA) ────────────────────────────────────────────
_JAVA_TOKENS = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])+'|//[^\n]*|/\*.*?\*/", re.S)
_SPRING_VERBS = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH"}
_ANNOTATION_ARGS = r"((?:[^()\"]|\"(?:\\.|[^\"\\])*\")*)"
_NON_PATH_ATTRS = re.compile(r"\b(?:produces|consumes|params|headers|name|method)\s*=\s*(?:\{[^}]*\}|\"(?:\\.|[^\"\\])*\"|[^,)]*)")


def _java_strip_comments(text: str) -> str:
    """Blank out comments (keeping strings and newlines), so a commented-out route or a Javadoc example is not a symbol."""
    return _JAVA_TOKENS.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else re.sub(r"[^\n]", " ", m.group(0)), text)


def _java_constants(text: str) -> dict[str, str]:
    """`static final String NAME = "/a" + OTHER + "/b";` constants of one file, so @GetMapping(NAME + "/{id}") resolves."""
    consts: dict[str, str] = {}
    for m in re.finditer(r"\bString\s+(\w+)\s*=\s*((?:\"(?:\\.|[^\"\\])*\"|\w+|\s|\+)+);", text):
        val = _java_eval(m.group(2), consts)
        if val is not None:
            consts[m.group(1)] = val
    return consts


def _java_eval(expr: str, consts: dict[str, str]) -> str | None:
    parts = re.findall(r"\"((?:\\.|[^\"\\])*)\"|(\w+)", expr)
    if not parts:
        return None
    out = ""
    for lit, name in parts:
        if name:
            if name not in consts:
                return None
            out += consts[name]
        else:
            out += lit
    return out


def _java_paths(args: str, consts: dict[str, str] | None = None) -> list[str]:
    """The path values of a mapping annotation: ("/a"), (value = "/a"), (path = {"/a", "/b"}), (BASE + "/a"); none means the class prefix itself."""
    args = _NON_PATH_ATTRS.sub("", args).strip().strip(",")
    args = re.sub(r"^\s*(?:value|path)\s*=\s*", "", args).strip()
    if args.startswith("{") and args.endswith("}"):
        args = args[1:-1]
    paths = []
    for item in re.findall(r"(?:\"(?:\\.|[^\"\\])*\"|[^,\"])+", args):
        val = _java_eval(item, consts or {}) if item.strip() else None
        if val is not None:
            paths.append(val)
    return paths or [""]


def _join_path(prefix: str, path: str) -> str:
    return "/" + "/".join(p.strip("/") for p in (prefix, path) if p and p.strip("/"))


def _java_routes(text: str, out: list) -> None:
    consts = _java_constants(text)
    decl = re.search(r"(?m)^[ \t]*(?:(?:public|protected|private|abstract|final|static|sealed)\s+)*(?:class|interface)\s+\w+", text)
    head, body = (text[:decl.start()], text[decl.start():]) if decl else ("", text)
    prefixes = [""]
    pm = list(re.finditer(r"@(?:RequestMapping|Path)\s*\(" + _ANNOTATION_ARGS + r"\)", head))
    if pm:
        prefixes = _java_paths(pm[-1].group(1), consts)
    # Spring: @GetMapping(...), @RequestMapping(method = RequestMethod.X ...)
    for m in re.finditer(r"@(Get|Post|Put|Delete|Patch)Mapping\b(?:\s*\(" + _ANNOTATION_ARGS + r"\))?", body):
        for pre in prefixes:
            for path in _java_paths(m.group(2) or "", consts):
                _add(out, "route", f"{_SPRING_VERBS[m.group(1)]} {_join_path(pre, path)}", m.group(0))
    for m in re.finditer(r"@RequestMapping\b(?:\s*\(" + _ANNOTATION_ARGS + r"\))?", body):
        args = m.group(1) or ""
        verbs = re.findall(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)", args) or ["ANY"]
        for pre in prefixes:
            for path in _java_paths(args, consts):
                for v in verbs:
                    _add(out, "route", f"{v} {_join_path(pre, path)}", m.group(0))
    # JAX-RS: @GET/@POST... on a method, optionally with its own @Path (before or after the verb)
    masked = re.sub(r"\"(?:\\.|[^\"\\\n])*\"", lambda x: '"' + " " * (len(x.group(0)) - 2) + '"', body)  # braces inside "/{id}" are not code
    for m in re.finditer(r"@(GET|POST|PUT|DELETE|PATCH|HEAD)\b", body):
        start = max(masked.rfind(c, 0, m.start()) for c in ";{}") + 1
        ends = [i for i in (masked.find("{", m.end()), masked.find(";", m.end())) if i >= 0]
        window = body[start:min(ends) if ends else m.end() + 300]
        own = re.search(r"@Path\s*\(" + _ANNOTATION_ARGS + r"\)", window)
        for pre in prefixes:
            for path in (_java_paths(own.group(1), consts) if own else [""]):
                _add(out, "route", f"{m.group(1)} {_join_path(pre, path)}", window.strip())


def _java_symbols(text: str, out: list) -> None:
    text = _java_strip_comments(text)
    for m in re.finditer(r"(?m)^[ \t]*((?:@\w+(?:\s*\([^)\n]*\))?\s+)*)public\s+(?:(?:abstract|final|sealed|non-sealed|static|strictfp)\s+)*(@interface|class|interface|enum|record)\s+(\w+)", text):
        annotations, kind, name = m.group(1), m.group(2), m.group(3)
        if kind == "record":
            brace = text.find("{", m.end())
            _add(out, "export", name, text[m.start():brace] if brace >= 0 else text[m.start():m.end() + 300])
        elif re.search(r"@(?:Entity|Embeddable|MappedSuperclass)\b", annotations):
            _add(out, "model", name, _block_of(text, m.start()))
        elif kind in ("interface", "enum", "@interface"):
            _add(out, "export", name, _block_of(text, m.start()))
        else:
            _add(out, "export", name, text[m.start():m.end() + 160].split("{")[0])
    for m in re.finditer(r"@Table\s*\([^)]*?\bname\s*=\s*\"([A-Za-z_]\w*)\"", text):
        _add(out, "model", m.group(1), "")
    _java_routes(text, out)
    # configuration: @Value("${a.b:default}"), @ConfigurationProperties("a.b"), Environment.getProperty("a.b"), System.getenv("X")
    for m in re.finditer(r"@Value\(\s*\"\$\{([A-Za-z_][\w.\-]*)", text):
        _add(out, "config", m.group(1), "")
    for m in re.finditer(r"@ConfigurationProperties\(\s*(?:(?:prefix|value)\s*=\s*)?\"([A-Za-z_][\w.\-]*)\"", text):
        _add(out, "config", m.group(1), "")
    for m in re.finditer(r"\b(?:getProperty|getRequiredProperty)\(\s*\"([A-Za-z_][\w.\-]*)\"", text):
        _add(out, "config", m.group(1), "")


def _config_file_symbols(file_path: str, text: str, out: list) -> None:
    """Spring application.properties / application.yml: every key is a setting, and `${ENV_VAR:default}` references are environment variables."""
    base = file_path.rsplit("/", 1)[-1]
    if not re.match(r"(?:application|bootstrap)(?:-[\w.-]+)?\.(?:properties|ya?ml)$", base):
        return
    keys: list[str] = []
    if base.endswith(".properties"):
        keys = re.findall(r"(?m)^\s*([A-Za-z_][\w.\-\[\]]*)\s*[=:]", text)
    else:
        stack: list[tuple[int, str]] = []
        for line in text.splitlines():
            m = re.match(r"^(\s*)([A-Za-z_][\w.\-]*)\s*:(.*)$", line)
            if not m:
                continue
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2)))
            if m.group(3).strip() and not m.group(3).strip().startswith("#"):
                keys.append(".".join(k for _, k in stack))
    for k in keys:
        _add(out, "config", k, "")
    for m in re.finditer(r"\$\{([A-Z][A-Z0-9_]{2,})", text):
        _add(out, "config", m.group(1), "")


def extract_public_symbols(file_path: str, text: str) -> list[dict]:
    out: list[dict] = []
    ext = (file_path.rsplit(".", 1)[-1] if "." in file_path else "").lower()
    is_js = bool(re.match(r"^(js|jsx|mjs|cjs|ts|tsx)$", ext)) or not file_path
    is_go, is_py, is_java = ext == "go", ext == "py", ext == "java"

    if is_js:
        for m in re.finditer(r"(?m)^\s*export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(\([^)]*\))?", text):
            _add(out, "export", m.group(1), m.group(0))
        for m in re.finditer(r"(?m)^\s*export\s+(?:declare\s+)?(?:abstract\s+)?(?:const|let|var|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)([^\n=]*)", text):
            _add(out, "export", m.group(1), m.group(0))
        for m in re.finditer(r"(?m)^\s*export\s*\{([^}]*)\}", text):
            for n in m.group(1).split(","):
                _add(out, "export", re.split(r"\s+as\s+", n.strip())[-1], "")
        for m in re.finditer(r"module\.exports\s*=\s*\{([^}]*)\}", text):
            for n in m.group(1).split(","):
                _add(out, "export", n.strip().split(":")[0], "")
        for m in re.finditer(r"\bexports\.([A-Za-z_$][\w$]*)\s*=", text):
            _add(out, "export", m.group(1), m.group(0))
        # HTTP routes (Express/Koa/Fastify style) and Next.js route handlers
        for m in re.finditer(r"\b(?:app|router|server|api)\.(get|post|put|patch|delete|all)\(\s*['\"`](/[^'\"`]*)['\"`]", text):
            _add(out, "route", f"{m.group(1).upper()} {m.group(2)}", m.group(0))
        if re.search(r"(^|/)route\.(ts|js)$", file_path):
            for m in re.finditer(r"export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE)\b", text):
                path = re.sub(r"/route\.(ts|js)$", "", re.sub(r".*/app", "", file_path)) or "/"
                _add(out, "route", f"{m.group(1)} {path}", m.group(0))
        # tRPC / RPC procedures
        for m in re.finditer(r"(?m)^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:[A-Za-z_$][\w$]*\.)*(?:publicProcedure|protectedProcedure|privateProcedure|authedProcedure|procedure|adminProcedure)\b", text):
            _add(out, "rpc", m.group(1), m.group(0))
    if is_go:
        for m in re.finditer(r"(?m)^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)\s*(\([^)]*\))?", text):
            _add(out, "export", m.group(1), m.group(0))
        for m in re.finditer(r"(?m)^type\s+([A-Z]\w*)\s+(struct|interface)", text):
            _add(out, "export", m.group(1), m.group(0))
        # net/http (Go 1.22) patterns: mux.HandleFunc("GET /api/health", h)
        for m in re.finditer(r"\.Handle(?:Func)?\(\s*\"(GET|POST|PUT|PATCH|DELETE)\s+(/[^\"]*)\"", text):
            _add(out, "route", f"{m.group(1)} {m.group(2)}", m.group(0))
    if is_py:
        for m in re.finditer(r"(?m)^(?:async\s+)?def\s+([A-Za-z]\w*)\s*(\([^)]*\))?", text):
            _add(out, "export", m.group(1), m.group(0))
        for m in re.finditer(r"(?m)^class\s+([A-Za-z]\w*)", text):
            _add(out, "export", m.group(1), m.group(0))
        for m in re.finditer(r"@(?:app|router|bp|blueprint)\.(?:route|get|post|put|patch|delete)\(\s*['\"](/[^'\"]*)['\"]", text):
            _add(out, "route", m.group(1), m.group(0))
    if is_java:
        _java_symbols(text, out)
    elif ext in ("properties", "yml", "yaml"):
        _config_file_symbols(file_path, text, out)
    # Schemas and contracts, any extension
    for m in re.finditer(r"(?m)^model\s+([A-Za-z_]\w*)\s*\{", text):
        _add(out, "model", m.group(1), _block_of(text, m.start()))
    for m in re.finditer(r"(?m)^enum\s+([A-Za-z_]\w*)\s*\{", text):
        _add(out, "model", m.group(1), _block_of(text, m.start()))
    for m in re.finditer(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`]?([A-Za-z_]\w*)[\"`]?", text, re.I):
        _add(out, "model", m.group(1), "")
    for m in re.finditer(r"(?m)^(?:message|service|rpc)\s+([A-Za-z_]\w*)", text):
        _add(out, "model", m.group(1), _block_of(text, m.start()))
    if re.search(r"openapi|swagger", file_path, re.I):
        for m in re.finditer(r"(?m)^\s{2}(/[\w/{}.-]+):", text):
            _add(out, "route", m.group(1), "")
    # Environment variables and config keys are a contract with operators
    for m in re.finditer(r"\b(?:process\.env|import\.meta\.env)\.([A-Z][A-Z0-9_]{2,})", text):
        _add(out, "config", m.group(1), "")
    for m in re.finditer(r"\b(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\(\s*|getenv\(\s*|os\.Getenv\(\s*)[\"']([A-Z][A-Z0-9_]{2,})[\"']", text):
        _add(out, "config", m.group(1), "")
    return out


def public_symbols(snapshot: str | None) -> dict[str, dict]:
    """All public symbols of a snapshot, keyed `kind:name`, with a signature string for change detection."""
    out: dict[str, dict] = {}
    for f in split_snapshot(snapshot):
        for s in extract_public_symbols(f["path"], f["text"]):
            key = f"{s['kind']}:{s['name']}"
            if key not in out:
                out[key] = {**s, "key": key, "paths": [f["path"]]}
            else:
                e = out[key]
                if f["path"] not in e["paths"]:
                    e["paths"].append(f["path"])
                e["sig"] = _norm(f"{e['sig']} {s['sig']}")
    return out


def diff_public_symbols(before: str | None, after: str | None) -> dict:
    """What changed in the public interface between two snapshots."""
    a, b = public_symbols(before), public_symbols(after)
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    changed = [k for k in b if k in a and a[k]["sig"] != b[k]["sig"]]

    def bare(k):
        return (b.get(k) or a.get(k) or {}).get("name")

    names = [n for n in dict.fromkeys(bare(k) for k in added + removed + changed) if n]
    return {"added": added, "removed": removed, "changed": changed, "names": names,
            "touched": bool(added or removed or changed), "total": len(b)}


def doc_tokens(text: str | None) -> set[str]:
    """Identifier-like tokens of a document: the candidates a doc may be referencing."""
    return set(re.findall(r"[A-Za-z_$][\w$]{2,}", str(text or "")))
