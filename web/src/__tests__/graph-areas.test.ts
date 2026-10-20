import { describe, expect, it } from "vitest";

import {
  AREA_PALETTE,
  areaColor,
  areaOf,
  areasForChips,
  groupAreas,
  parentMap,
} from "@/lib/graph/areas";

describe("areaOf", () => {
  it("is the module itself, and a file walks owns until it hits one", () => {
    const kindOf = { web: "module", "web/a.py": "file", "web/a.py::foo": "symbol" };
    const parentOf = parentMap([
      { src: "web", dst: "web/a.py" },
      { src: "web/a.py", dst: "web/a.py::foo" },
    ]);
    expect(areaOf("web", "module", parentOf, kindOf)).toBe("web");
    expect(areaOf("web/a.py", "file", parentOf, kindOf)).toBe("web");
    expect(areaOf("web/a.py::foo", "symbol", parentOf, kindOf)).toBe("web");
  });

  it("falls back to the directory when nothing owns the node", () => {
    expect(areaOf("web/src/a.ts", "file", {}, { "web/src/a.ts": "file" })).toBe("web/src");
    expect(areaOf("web/src/a.ts::foo", "symbol", {}, {})).toBe("web/src");
  });

  it("does not invent a shared root for a bare filename", () => {
    expect(areaOf("a.py", "file", {}, { "a.py": "file" })).toBe("a.py");
  });

  it("does not follow a cycle", () => {
    const parentOf = { a: "b", b: "a" };
    expect(areaOf("a", "file", parentOf, { a: "file", b: "file" })).toBe("a");
  });
});

describe("parentMap", () => {
  it("is stable under edge reorder", () => {
    const owns = [
      { src: "web", dst: "web/a.py" },
      { src: "lib", dst: "lib/b.py" },
    ];
    expect(parentMap(owns)).toEqual(parentMap([...owns].reverse()));
  });
});

describe("areaColor", () => {
  it("picks only from the existing token palette", () => {
    for (const id of ["web", "lib", "backend/app/services", "docs"]) {
      expect(AREA_PALETTE).toContain(areaColor(id));
    }
  });

  it("is stable, and two names can differ", () => {
    expect(areaColor("web")).toBe(areaColor("web"));
    expect(areaColor("web")).not.toBe(areaColor("lib"));
  });
});

describe("areasForChips", () => {
  it("names modules when there are two, not every orphan file", () => {
    const groups = groupAreas(
      ["web", "web/a.py", "lib", "solo.py"],
      { web: "web", "web/a.py": "web", lib: "lib", "solo.py": "solo.py" },
    );
    const chips = areasForChips(groups, { web: "module", lib: "module", "web/a.py": "file" });
    expect(chips.map((c) => c.id)).toEqual(["lib", "web"]);
  });
});
