/**
 * Seeded scale fixtures (PRD-52 D9).
 *
 * `sparse` is a port of the design's `bigData` (Build.dc.html, seed 7331):
 * the same areas, the same hash RNG, the same power-law edges. Module paths
 * are the design's area labels, which is what `areasAtScale` groups on.
 * `dense` is the hairball: 2,500 nodes, 25,000 edges, 30% of them between areas.
 */

import type { ScaleEdge, ScaleNode } from "../areas";

export interface BigMap {
  name: "sparse" | "dense";
  nodes: ScaleNode[];
  edges: ScaleEdge[];
  owns: ScaleEdge[];
}

/** The design's mulberry-style hash. Same sequence as `bigData` for seed 7331. */
function hashRng(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildSparse(): BigMap {
  const rnd = hashRng(7331);
  const pk = <T>(a: T[]): T => a[Math.floor(rnd() * a.length)];
  const pw = <T>(a: T[]): T => a[Math.floor(Math.pow(rnd(), 2.4) * a.length)];
  const NOUN = ["Item", "Wave", "Agent", "Lesson", "Shard", "Route", "Cluster", "Seat", "Key", "Usage", "Prd", "Link", "Node", "Edge", "Graph", "Outpost", "Host", "Memory", "Judge", "Claim", "Review", "Trace", "Span", "Token", "Session", "Project", "Tracker", "Roadmap", "Request", "Triage", "Harness", "Spawn", "Tier", "Mix", "Budget", "Export", "Org", "Team", "Member", "Invite", "Webhook", "Sync", "Diff", "Patch", "Search", "Filter", "Badge", "Event", "Touchpoint", "Evidence"];
  const SUF = ["View", "Panel", "Table", "Card", "Row", "List", "Form", "Dialog", "Drawer", "Header", "Toolbar", "Menu", "Chip", "Empty", "Skeleton", "Grid", "Tabs", "Picker", "Editor", "Inspector"];
  const UIW = ["Button", "IconButton", "Input", "Select", "Combobox", "Tooltip", "Popover", "Dialog", "Sheet", "Tabs", "Badge", "Avatar", "Kbd", "Switch", "Checkbox", "Radio", "Slider", "Toast", "Table", "DataGrid", "Skeleton", "Spinner", "Menu", "ContextMenu", "Breadcrumbs", "Pagination", "ScrollArea", "Separator", "Card", "EmptyState", "CodeBlock", "Markdown", "SplitPane", "Resizable", "Calendar", "DatePicker", "Chart", "Sparkline", "ProgressBar", "Stepper"];
  const VERB = ["get", "list", "claim", "update", "register", "search", "link", "spawn", "end", "post", "resolve", "build", "format", "parse", "group", "sort", "select", "merge"];
  const snake = (w: string) => w.replace(/([a-z])([A-Z])/g, "$1_$2").toLowerCase();
  const low = (w: string) => w[0].toLowerCase() + w.slice(1);
  let mig = 0;
  let prd = 20;
  let adr = 0;
  const F: Record<string, () => string> = {
    shell: () => pk(NOUN) + pk(["Nav", "Frame", "Bar", "Rail", "Palette", "Switcher", "Crumbs", "Layout", "Shell"]) + ".tsx",
    ui: () => pk(UIW) + pk(["", "", "Group", "Item", "Trigger", "Content", "Root", "Label"]) + ".tsx",
    feat: () => (rnd() < 0.7 ? pk(NOUN) + pk(SUF) + ".tsx" : low(pk(NOUN)) + pk(["Store", "Selectors", "Machine", "Utils", "Queries"]) + ".ts"),
    lib: () => low(pk(NOUN)) + pk(["Utils", "Client", "Format", "Schema", "Cache", "Paths", "Errors", "Parse"]) + ".ts",
    hooks: () => "use" + pk(NOUN) + pk(["", "s", "Query", "Mutation", "State", "Stream"]) + ".ts",
    routers: () => snake(pk(NOUN)) + pk(["s", "", "_admin", "_public", "_v2"]) + ".py",
    services: () => snake(pk(NOUN)) + "_" + pk(["service", "repo", "tasks", "policy", "jobs", "events"]) + ".py",
    models: () => snake(pk(NOUN)) + pk(["", "_schema", "_row", "_enum", "_dto"]) + ".py",
    db: () => snake(pk(NOUN)) + "_" + pk(["index", "view", "fn", "seed", "trigger"]) + ".sql",
    migr: () => String(++mig).padStart(4, "0") + "_" + pk(["add", "drop", "alter", "index", "backfill"]) + "_" + snake(pk(NOUN)) + "s.py",
    mcp: () => pk(VERB) + "_" + snake(pk(NOUN)) + ".py",
    cli: () => "cmd_" + snake(pk(NOUN)) + ".py",
    tests: () => (rnd() < 0.6 ? pk(NOUN) + pk(SUF) + ".test.tsx" : "test_" + snake(pk(NOUN)) + "_" + pk(["service", "router", "model", "flow"]) + ".py"),
    docs: () => (rnd() < 0.5 ? "PRD-" + ++prd + ".md" : "adr-" + String(++adr).padStart(3, "0") + "-" + snake(pk(NOUN)).replace(/_/g, "-") + ".md"),
  };
  const SYM: Record<string, () => string> = {
    ts: () => {
      const r = rnd();
      return r < 0.3 ? "use" + pk(NOUN) + "()" : r < 0.65 ? pk(VERB) + pk(NOUN) + "()" : pk(NOUN) + pk(["Props", "State", "Row", "Config", "Context"]);
    },
    py: () => (rnd() < 0.6 ? pk(VERB) + "_" + snake(pk(NOUN)) + "()" : pk(NOUN) + pk(["Model", "Schema", "Repo", "Service", "Error"])),
  };
  const CONFIG = ["vite.config.ts", "tailwind.config.ts", "tsconfig.json", "package.json", "pyproject.toml", "alembic.ini", ".eslintrc.cjs", "docker-compose.yml", "Dockerfile", "Makefile", "ruff.toml", "vitest.config.ts", "playwright.config.ts", ".env.example", "renovate.json", "codecov.yml", "pnpm-workspace.yaml", "biome.json", "fly.toml", "Procfile", "requirements.txt", "uv.lock"];
  // id, label, count, deps, file gen, symbol lang, path prefix. Group is unused; the design carries it for colour.
  const AREAS: [string, string, number, string[], string, string, string][] = [
    ["shell", "components/shell", 64, ["ui", "lib", "hooks", "plan"], "shell", "ts", "web/src/"],
    ["plan", "features/plan", 196, ["ui", "lib", "hooks"], "feat", "ts", "web/src/"],
    ["buildf", "features/build", 142, ["ui", "lib", "hooks", "graph"], "feat", "ts", "web/src/"],
    ["graph", "features/graph", 128, ["ui", "lib", "hooks"], "feat", "ts", "web/src/"],
    ["fleet", "features/fleet", 156, ["ui", "lib", "hooks"], "feat", "ts", "web/src/"],
    ["memory", "features/memory", 112, ["ui", "lib", "hooks"], "feat", "ts", "web/src/"],
    ["observe", "features/observe", 118, ["ui", "lib", "hooks"], "feat", "ts", "web/src/"],
    ["ui", "components/ui", 168, ["lib"], "ui", "ts", "web/src/"],
    ["hooks", "hooks", 54, ["lib"], "hooks", "ts", "web/src/"],
    ["lib", "lib", 92, [], "lib", "ts", "web/src/"],
    ["mcp", "mcp/tools", 72, ["services", "models"], "mcp", "py", "server/"],
    ["cli", "cli", 62, ["services", "models"], "cli", "py", ""],
    ["routers", "api/routers", 164, ["services", "models"], "routers", "py", "server/"],
    ["services", "api/services", 188, ["models", "db"], "services", "py", "server/"],
    ["models", "api/models", 84, ["db"], "models", "py", "server/"],
    ["migr", "db/migrations", 126, ["db"], "migr", "", "server/"],
    ["db", "db", 32, [], "db", "py", "server/"],
    ["tests", "__tests__", 172, [], "tests", "", "web/src/"],
    ["docs", "docs", 58, [], "docs", "", ""],
    ["config", "config", 22, [], "", "", ""],
  ];

  interface Area {
    id: string;
    label: string;
    n: number;
    deps: string[];
    fk: string;
    lang: string;
    pre: string;
    files: string[];
    syms: string[];
  }

  const used = new Set<string>();
  const uniq = (gen: () => string) => {
    for (let t = 0; t < 30; t++) {
      const name = gen();
      if (!used.has(name)) {
        used.add(name);
        return name;
      }
    }
    const b = gen();
    let j = 2;
    let name = "";
    do {
      name = b.replace(/(\(\)|\.[a-z.]+)?$/, j + "$1");
      j += 1;
    } while (used.has(name));
    used.add(name);
    return name;
  };

  const areas: Area[] = AREAS.map(([id, label, n, deps, fk, lang, pre]) => ({
    id, label, n, deps, fk, lang, pre, files: [], syms: [],
  }));
  const byId = Object.fromEntries(areas.map((a) => [a.id, a]));
  const nodes: ScaleNode[] = [];
  const owns: ScaleEdge[] = [];
  const filePath = new Map<string, string>();

  const add = (id: string, kind: string, path: string) => {
    nodes.push({ id, path, kind });
  };

  for (const a of areas) {
    used.add(a.label);
    // The label is the area home. The descriptive path is only the node's sort key.
    add(a.label, "module", a.label);
    if (a.id === "config") {
      for (const c of CONFIG.slice(0, a.n - 1)) {
        used.add(c);
        add(c, "config", c);
        a.files.push(c);
        owns.push({ src: a.label, dst: c, type: "owns" });
      }
      continue;
    }
    const nSym = a.lang ? Math.round((a.n - 1) * (a.id === "db" ? 0.2 : 0.36)) : 0;
    const nFile = a.n - 1 - nSym;
    for (let j = 0; j < nFile; j++) {
      const f = uniq(F[a.fk]);
      const path = a.id === "docs" ? "docs/" + f : a.pre + a.label + "/" + f;
      add(f, a.id === "docs" ? "doc" : "file", path);
      filePath.set(f, path);
      a.files.push(f);
      owns.push({ src: a.label, dst: f, type: "owns" });
    }
    for (let j = 0; j < nSym; j++) {
      const parent = pw(a.files);
      const sy = uniq(SYM[a.lang]);
      const path = (filePath.get(parent) ?? parent) + "::" + sy;
      add(sy, "symbol", path);
      a.syms.push(sy);
      owns.push({ src: parent, dst: sy, type: "owns" });
    }
  }

  const typed: ScaleEdge[] = [];
  const seen = new Set<string>();
  const link = (x: string | undefined, y: string | undefined, t: string) => {
    if (!x || !y || x === y) return;
    const k = x + "|" + y + "|" + t;
    if (seen.has(k)) return;
    seen.add(k);
    typed.push({ src: x, dst: y, type: t });
  };
  const code = areas.filter((a) => a.files.length && !["tests", "docs", "config"].includes(a.id));
  const tgt = (a: Area) => {
    const r = rnd();
    if (r < 0.015) return pk(code);
    if (!a.deps.length || r < 0.55) return a;
    return byId[pk(a.deps)];
  };
  for (const a of code) {
    for (const f of a.files) {
      const n = 1 + Math.floor(-Math.log(1 - rnd()) * 1.5);
      for (let j = 0; j < n; j++) link(f, pw(tgt(a).files), "imports");
      const m = Math.floor(rnd() * 2.2);
      for (let j = 0; j < m; j++) {
        const ta = tgt(a);
        if (ta.syms.length) link(f, pw(ta.syms), "calls");
      }
    }
  }
  for (const t of byId.tests.files) {
    const k = rnd() < 0.4 ? 2 : 1;
    for (let j = 0; j < k; j++) link(pw(pk(code).files), t, "tested_by");
  }
  for (const d of byId.docs.files) {
    const k = 1 + Math.floor(rnd() * 3);
    for (let j = 0; j < k; j++) link(pw(pk(code).files), d, "references");
  }
  for (const c of byId.config.files) {
    const k = 2 + Math.floor(rnd() * 6);
    for (let j = 0; j < k; j++) link(pw(pk(code).files), c, "references");
  }

  return { name: "sparse", nodes, owns, edges: owns.concat(typed) };
}

function buildDense(): BigMap {
  const rnd = hashRng(0x51e5e);
  const areaCount = 20;
  const perArea = 125;
  const nodes: ScaleNode[] = [];
  const owns: ScaleEdge[] = [];
  const files: string[][] = [];
  for (let a = 0; a < areaCount; a++) {
    const label = `area/a${String(a).padStart(2, "0")}`;
    nodes.push({ id: label, path: label, kind: "module" });
    const list: string[] = [];
    for (let i = 0; i < perArea - 1; i++) {
      const id = `${label}/f${String(i).padStart(3, "0")}`;
      nodes.push({ id, path: id, kind: "file" });
      owns.push({ src: label, dst: id, type: "owns" });
      list.push(id);
    }
    files.push(list);
  }
  const pick = (list: string[]) => list[Math.floor(rnd() * list.length)];
  const typeOf = (r: number) =>
    r < 0.8 ? "imports" : r < 0.95 ? "calls" : r < 0.98 ? "tested_by" : "references";
  const typed: ScaleEdge[] = [];
  for (let i = 0; i < 15020; i++) {
    const a = Math.floor(rnd() * areaCount);
    const src = pick(files[a]);
    let dst = pick(files[a]);
    if (src === dst) dst = files[a][(files[a].indexOf(src) + 1) % files[a].length];
    typed.push({ src, dst, type: typeOf(rnd()) });
  }
  for (let i = 0; i < 7500; i++) {
    const a = Math.floor(rnd() * areaCount);
    let b = Math.floor(rnd() * (areaCount - 1));
    if (b >= a) b += 1;
    typed.push({ src: pick(files[a]), dst: pick(files[b]), type: typeOf(rnd()) });
  }
  return { name: "dense", nodes, owns, edges: owns.concat(typed) };
}

export const sparse: BigMap = buildSparse();
export const dense: BigMap = buildDense();

/** Module label (`area/a00`) of a dense node id. */
export function denseArea(id: string): string {
  const parts = id.split("/");
  return `${parts[0]}/${parts[1]}`;
}
