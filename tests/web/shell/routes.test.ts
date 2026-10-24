import { describe, expect, it } from "vitest";
import { pageFromLocation, pageSearch, PAGES } from "../../../web/src/shell/routes";

describe("page routing", () => {
  it("routes ?view= to each of the seven pages", () => {
    for (const page of PAGES) expect(pageFromLocation(pageSearch(page))).toBe(page);
    expect(pageFromLocation("?view=Chart")).toBe("chart");
    expect(pageSearch("today")).toBe("");
    expect(pageSearch("macro")).toBe("?view=macro");
  });

  it("maps the old deep links onto the new pages", () => {
    expect(pageFromLocation("?section=overview")).toBe("chart");
    expect(pageFromLocation("?section=macro")).toBe("macro");
    expect(pageFromLocation("?section=Calendar")).toBe("calendar");
    expect(pageFromLocation("?section=news")).toBe("news");
    expect(pageFromLocation("?view=details")).toBe("chart");
    expect(pageFromLocation("?view=learn")).toBe("learn");
    expect(pageFromLocation("?view=playbook")).toBe("playbook");
  });

  it("opens Today for retired, unknown or missing pages", () => {
    for (const old of ["analysts", "briefings", "history", "technical", "bogus"]) {
      expect(pageFromLocation(`?section=${old}`)).toBe("today");
    }
    expect(pageFromLocation("")).toBe("today");
    expect(pageFromLocation("?view=bogus")).toBe("today");
    expect(pageFromLocation("?note=release:1")).toBe("today");
  });
});
