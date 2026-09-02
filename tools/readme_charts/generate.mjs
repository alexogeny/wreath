import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { animate } from "animejs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../..");
const SOURCE = resolve(
  ROOT,
  "benchmarks/baselines/e2e-holistic-stack-instructions.json",
);
const OUTPUT = resolve(ROOT, "docs/assets/readme");
const CHECK = process.argv.includes("--check");
const sourceBytes = await readFile(SOURCE);
const sourceHash = createHash("sha256").update(sourceBytes).digest("hex");
const report = JSON.parse(sourceBytes);

const STACKS = [
  ["wreath-optimal", "Wreath · DCZ", "#7c3aed"],
  ["wreath", "Wreath · gzip", "#d946ef"],
  ["blacksheep", "BlackSheep", "#2563eb"],
  ["fastapi", "FastAPI", "#0891b2"],
  ["sanic", "Sanic", "#059669"],
];
const STAGES = ["ready", "verified", "warmed", "retained"];
const WIDTH = 1120;

function escapeXml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fixed(value) {
  return Number(value.toFixed(3)).toString();
}

function mib(value) {
  return value / (1024 * 1024);
}

function animationValues(finalValue, delay, duration = 900, initialValue = 0) {
  const state = { value: initialValue };
  const motion = animate(state, {
    value: finalValue,
    delay,
    duration,
    ease: "out(4)",
    autoplay: false,
  });
  const end = delay + duration;
  const samples = [];
  for (let index = 0; index <= 16; index += 1) {
    motion.seek((end * index) / 16, true);
    samples.push(fixed(state.value));
  }
  motion.cancel();
  return samples.join(";");
}

function shell(title, subtitle, height, body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${height}" viewBox="0 0 ${WIDTH} ${height}" role="img" aria-labelledby="title desc" data-source-sha256="${sourceHash}" data-generator="animejs-4.5.0">
  <title id="title">${escapeXml(title)}</title>
  <desc id="desc">${escapeXml(subtitle)}</desc>
  <defs>
    <linearGradient id="paper" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#fff7ff"/>
      <stop offset="0.52" stop-color="#f5f3ff"/>
      <stop offset="1" stop-color="#eff6ff"/>
    </linearGradient>
    <filter id="shadow" x="-10%" y="-20%" width="120%" height="150%">
      <feDropShadow dx="0" dy="8" stdDeviation="12" flood-color="#6d28d9" flood-opacity=".10"/>
    </filter>
  </defs>
  <rect x="12" y="12" width="1096" height="${height - 24}" rx="28" fill="url(#paper)" stroke="#ddd6fe" filter="url(#shadow)"/>
  <text x="58" y="70" fill="#2e1065" font-family="ui-sans-serif, system-ui, sans-serif" font-size="28" font-weight="750">${escapeXml(title)}</text>
  <text x="58" y="101" fill="#6b7280" font-family="ui-sans-serif, system-ui, sans-serif" font-size="15">${escapeXml(subtitle)}</text>
${body}
  <text x="1062" y="${height - 35}" text-anchor="end" fill="#8b5cf6" font-family="ui-monospace, monospace" font-size="11">retained · ${escapeXml(report.recorded)}</text>
</svg>
`;
}

function instructionChart() {
  const left = 238;
  const chartWidth = 810;
  const minLog = 6;
  const maxLog = Math.log10(256_000_000);
  const xFor = (value) => left + ((Math.log10(value) - minLog) / (maxLog - minLog)) * chartWidth;
  const ticks = [1, 4, 16, 64, 256];
  const grid = ticks
    .map((tick) => {
      const x = xFor(tick * 1_000_000);
      return `  <line x1="${fixed(x)}" y1="139" x2="${fixed(x)}" y2="454" stroke="#c4b5fd" stroke-opacity=".48"/>
  <text x="${fixed(x)}" y="477" text-anchor="middle" fill="#6b7280" font-family="ui-monospace, monospace" font-size="12">${tick}M</text>`;
    })
    .join("\n");
  const rows = STACKS.map(([key, label, color], index) => {
    const value = report.arms[key].holistic.median;
    const y = 151 + index * 59;
    const finalWidth = xFor(value) - left;
    const values = animationValues(finalWidth, index * 95);
    return `  <text x="208" y="${y + 24}" text-anchor="end" fill="#312e81" font-family="ui-sans-serif, system-ui, sans-serif" font-size="15" font-weight="650">${escapeXml(label)}</text>
  <rect x="${left}" y="${y}" width="${fixed(finalWidth)}" height="34" rx="17" fill="${color}" opacity=".90" data-stack="${key}" data-median-instructions="${value.toFixed(3)}">
    <animate attributeName="width" values="${values}" keyTimes="0;.0625;.125;.1875;.25;.3125;.375;.4375;.5;.5625;.625;.6875;.75;.8125;.875;.9375;1" dur="1.5s" fill="freeze"/>
  </rect>
  <text x="${fixed(xFor(value) + 12)}" y="${y + 23}" fill="#312e81" font-family="ui-monospace, monospace" font-size="13" font-weight="700">${(value / 1_000_000).toFixed(2)}M</text>`;
  }).join("\n");
  return shell(
    "One complete request · retired instructions",
    "TLS 1.3, policy, typed input, auth, Cedar, PostgreSQL, HTTP, analytics, templates and compression · log scale · lower is better",
    520,
    `${grid}\n${rows}`,
  );
}

function memoryChart() {
  const left = 114;
  const top = 146;
  const chartWidth = 690;
  const chartHeight = 305;
  const stageX = (index) => left + index * (chartWidth / (STAGES.length - 1));
  const yFor = (value) => top + chartHeight - ((value - 60) / 52) * chartHeight;
  const ticks = [60, 70, 80, 90, 100, 110];
  const grid = ticks
    .map((tick) => {
      const y = yFor(tick);
      return `  <line x1="${left}" y1="${fixed(y)}" x2="${left + chartWidth}" y2="${fixed(y)}" stroke="#c4b5fd" stroke-opacity=".42"/>
  <text x="94" y="${fixed(y + 4)}" text-anchor="end" fill="#6b7280" font-family="ui-monospace, monospace" font-size="12">${tick} MiB</text>`;
    })
    .join("\n");
  const labels = STAGES.map(
    (stage, index) => `  <text x="${fixed(stageX(index))}" y="480" text-anchor="middle" fill="#6b7280" font-family="ui-sans-serif, system-ui, sans-serif" font-size="12">${stage}</text>`,
  ).join("\n");
  const paths = STACKS.map(([key, label, color], index) => {
    const values = STAGES.map((stage) => mib(report.memory[key][stage].pss_bytes.median));
    const points = values.map((value, point) => [stageX(point), yFor(value)]);
    const path = points.map(([x, y], point) => `${point ? "L" : "M"}${fixed(x)} ${fixed(y)}`).join(" ");
    const peakRss = mib(report.memory[key].observed_peak.rss_bytes.median);
    const draw = animationValues(0, index * 110, 1000, 1);
    const dots = points
      .map(([x, y], point) => `  <circle cx="${fixed(x)}" cy="${fixed(y)}" r="5" fill="#fff" stroke="${color}" stroke-width="3">
    <animate attributeName="opacity" values="0;0;1;1" keyTimes="0;.35;.75;1" dur="${fixed(1.1 + index * 0.11)}s" fill="freeze"/>
  </circle>`)
      .join("\n");
    return `  <path d="${path}" pathLength="1" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="1" stroke-dashoffset="0" data-stack="${key}" data-pss-mib="${values.map((value) => value.toFixed(3)).join(",")}" data-peak-rss-mib="${peakRss.toFixed(3)}">
    <animate attributeName="stroke-dashoffset" values="${draw}" keyTimes="0;.0625;.125;.1875;.25;.3125;.375;.4375;.5;.5625;.625;.6875;.75;.8125;.875;.9375;1" dur="${fixed(1.2 + index * 0.1)}s" fill="freeze"/>
  </path>
${dots}
  <circle cx="846" cy="${158 + index * 57}" r="6" fill="${color}"/>
  <text x="865" y="${163 + index * 57}" fill="#312e81" font-family="ui-sans-serif, system-ui, sans-serif" font-size="14" font-weight="650">${escapeXml(label)}</text>
  <text x="865" y="${182 + index * 57}" fill="#6b7280" font-family="ui-monospace, monospace" font-size="11">peak RSS ${peakRss.toFixed(2)} MiB</text>`;
  }).join("\n");
  return shell(
    "Process-tree memory through the request lifecycle",
    "PSS apportions shared mappings; side labels retain summed peak RSS · five fresh-process medians · lower is better",
    530,
    `${grid}\n${labels}\n${paths}`,
  );
}

const assets = new Map([
  ["holistic-instructions.svg", instructionChart()],
  ["holistic-memory.svg", memoryChart()],
]);

let stale = false;
for (const [name, content] of assets) {
  const path = resolve(OUTPUT, name);
  if (CHECK) {
    let current = "";
    try {
      current = await readFile(path, "utf8");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (current !== content) {
      console.error(`${name} is stale`);
      stale = true;
    }
  } else {
    await writeFile(path, content);
    console.log(`wrote ${path}`);
  }
}

if (stale) process.exitCode = 1;
