const fs = require("fs");
const path = require("path");

function fail(msg) {
  process.stderr.write(String(msg) + "\n");
  process.exit(1);
}

function bumpPatch(version) {
  const m = String(version).trim().match(/^(\d+)\.(\d+)\.(\d+)(.*)$/);
  if (!m) return null;
  const major = Number(m[1]);
  const minor = Number(m[2]);
  const patch = Number(m[3]);
  const suffix = m[4] || "";
  if ([major, minor, patch].some((n) => Number.isNaN(n))) return null;
  return `${major}.${minor}.${patch + 1}${suffix}`;
}

const pkgPath = path.join(process.cwd(), "package.json");
if (!fs.existsSync(pkgPath)) {
  fail(`package.json not found at: ${pkgPath}`);
}

const raw = fs.readFileSync(pkgPath, "utf-8");
const pkg = JSON.parse(raw);
const next = bumpPatch(pkg.version);
if (!next) {
  fail(`Invalid version: ${pkg.version}`);
}

pkg.version = next;
fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf-8");
process.stdout.write(next + "\n");

