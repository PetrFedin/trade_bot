/* Real browser-script interactions without external JS dependencies or network. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const HTML = fs.readFileSync(
  path.join(__dirname, "..", "docs", "research-ops-console.html"), "utf8"
);
const SCRIPT = HTML.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(SCRIPT, "the generated console must include its offline interaction script");

function harness() {
  const handlers = {};
  let exportedBlob = null;
  let download = null;
  const rows = Array.from(
    HTML.matchAll(/<tr id="cap-([^"]+)" data-state="([^"]+)">([\s\S]*?)<\/tr>/g),
    (match) => {
      const cells = Array.from(
        match[3].matchAll(/<td(?:\s[^>]*)?>([\s\S]*?)<\/td>/g),
        (cell) => ({
          innerText: cell[1].replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim(),
        })
      );
      return {
        dataset: { state: match[2] },
        cells,
        textContent: match[1] + " " + match[2] + " " + cells.map((c) => c.innerText).join(" "),
        hidden: false,
      };
    }
  );
  assert.equal(rows.length, 12, "expected all repository-backed capability records");

  const elements = {};
  function element(id, extra = {}) {
    return Object.assign({
      id,
      textContent: "",
      hidden: false,
      addEventListener(name, handler) {
        (handlers[id] ||= {})[name] = handler;
      },
    }, extra);
  }
  const values = {
    gross: "", turnover: "", fees: "", slippage: "", funding: "",
  };
  elements["capability-table"] = element("capability-table", {
    tBodies: [{ rows }],
  });
  elements["feature-search"] = element("feature-search", { value: "" });
  elements["feature-state"] = element("feature-state", { value: "ALL" });
  elements["feature-count"] = element("feature-count");
  elements["export-features"] = element("export-features");
  elements["scenario-form"] = element("scenario-form", {
    elements: {
      namedItem(name) {
        return { value: values[name] };
      },
    },
  });
  elements["scenario-error"] = element("scenario-error", { hidden: true });
  elements["scenario-results"] = element("scenario-results", { hidden: true });
  elements["scenario-base"] = element("scenario-base");
  elements["scenario-rows"] = element("scenario-rows", {
    children: [],
    replaceChildren() {
      this.children = [];
    },
    appendChild(value) {
      this.children.push(value);
    },
  });
  const document = {
    getElementById: (id) => {
      assert.ok(elements[id], "unexpected HTML element access: " + id);
      return elements[id];
    },
    createElement: (tag) => ({
      tagName: tag,
      children: [],
      textContent: "",
      appendChild(value) {
        this.children.push(value);
      },
      click() {
        if (tag === "a") {
          download = { href: this.href, filename: this.download };
        }
      },
      remove() {},
    }),
    body: { append() {} },
  };
  const url = {
    createObjectURL(blob) {
      exportedBlob = blob;
      return "blob:research-console-test";
    },
    revokeObjectURL() {},
  };
  vm.runInNewContext(SCRIPT, {
    document, URL: url, Blob, BigInt, String, Error,
  }, { timeout: 2000 });

  function submit(payload) {
    Object.assign(values, payload);
    let prevented = false;
    handlers["scenario-form"].submit({
      preventDefault() { prevented = true; },
    });
    assert.equal(prevented, true, "scenario must not navigate or submit to a server");
    return {
      error: elements["scenario-error"],
      result: elements["scenario-results"],
      base: elements["scenario-base"].textContent,
      rows: elements["scenario-rows"].children.map(
        (row) => row.children.map((cell) => cell.textContent)
      ),
    };
  }
  return {
    rows, elements, handlers, submit,
    getExport() { return { blob: exportedBlob, download }; },
  };
}

test("manual cost stress uses signed funding and correct four cost bands", () => {
  const h = harness();
  const result = h.submit({
    gross: "1000.00", turnover: "20000",
    fees: "12", slippage: "8", funding: "5",
  });
  assert.equal(result.error.hidden, true);
  assert.equal(result.result.hidden, false);
  assert.equal(result.base, "975.00 USDT / модельный Net P&L");
  assert.deepEqual(result.rows, [
    ["0 bps", "0.00 USDT", "975.00 USDT"],
    ["5 bps", "10.00 USDT", "965.00 USDT"],
    ["10 bps", "20.00 USDT", "955.00 USDT"],
    ["25 bps", "50.00 USDT", "925.00 USDT"],
  ]);
});

test("sub-cent stress rounding keeps full precision until half-even", () => {
  const h = harness();
  const positive = h.submit({
    gross: "0", turnover: "10.00000001",
    fees: "0", slippage: "0", funding: "0",
  });
  assert.equal(positive.rows[1][1], "0.01 USDT");
  assert.equal(positive.rows[1][2], "-0.01 USDT");
  const midpoint = h.submit({
    gross: "0", turnover: "10",
    fees: "0", slippage: "0", funding: "0",
  });
  assert.equal(midpoint.rows[1][1], "0.00 USDT");
  assert.equal(midpoint.rows[1][2], "0.00 USDT");
});

test("funding credit is signed and does not create a broker signal", () => {
  const h = harness();
  const result = h.submit({
    gross: "0", turnover: "1000", fees: "2",
    slippage: "1", funding: "-5",
  });
  assert.equal(result.base, "2.00 USDT / модельный Net P&L");
  assert.ok(HTML.includes("NO EXECUTION AUTHORITY"));
  assert.equal(HTML.includes("orderSubmit("), false);
});

test("failed validation removes prior results instead of displaying stale net", () => {
  const h = harness();
  h.submit({
    gross: "100", turnover: "1000", fees: "1",
    slippage: "1", funding: "0",
  });
  const invalid = h.submit({
    gross: "100", turnover: "1000", fees: "-1",
    slippage: "1", funding: "0",
  });
  assert.equal(invalid.result.hidden, true);
  assert.equal(invalid.error.hidden, false);
  assert.match(invalid.error.textContent, /комиссии и проскальзывание/);
});

test("invalid high-precision and exponent amounts cannot pass", () => {
  const h = harness();
  for (const gross of ["NaN", "Infinity", "1e12", "1.000000001", ""]) {
    const result = h.submit({
      gross, turnover: "1000", fees: "0",
      slippage: "0", funding: "0",
    });
    assert.equal(result.error.hidden, false, gross);
    assert.equal(result.result.hidden, true, gross);
  }
});

test("status/search filters use actual registry rows", () => {
  const h = harness();
  h.elements["feature-state"].value = "PLANNED";
  h.handlers["feature-state"].change();
  assert.equal(h.rows.filter((row) => !row.hidden).length, 6);
  h.elements["feature-search"].value = "MARK PRICE";
  h.elements["feature-state"].value = "ALL";
  h.handlers["feature-search"].input();
  h.handlers["feature-state"].change();
  assert.equal(h.rows.filter((row) => !row.hidden).length, 1);
  assert.equal(h.elements["feature-count"].textContent, "Показано функций: 1 из 12");
});

test("CSV export includes only currently visible capability rows", async () => {
  const h = harness();
  h.elements["feature-state"].value = "BLOCKED_SOURCE";
  h.handlers["feature-state"].change();
  h.handlers["export-features"].click();
  const { blob, download } = h.getExport();
  assert.ok(blob);
  assert.equal(download.filename, "astra_research_capabilities.csv");
  const csv = await blob.text();
  assert.match(csv, /Функция;?"/);
  assert.match(csv, /MARK PRICE FUNDING PROVENANCE/);
  assert.equal(csv.includes("GRID DCA SCENARIO LAB"), false);
  assert.equal(csv.split("\r\n").length, 2);
});
