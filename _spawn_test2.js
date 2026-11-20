const { spawnSync } = require("child_process");
const shim = "C:\\Users\\MI\\Documents\\deepseek-harness\\default-workspace\\screen-qa\\tools\\cli_shims\\claude.exe";
const shim2 = "C:\\Users\\MI\\Documents\\deepseek-harness\\default-workspace\\screen-qa\\tools\\cli_shims\\codex.exe";
for (const [label, exe, args] of [["工作区里的 claude.exe 垫片", shim, ["--version"]],
                                 ["工作区里的 codex.exe 垫片", shim2, ["--version"]]]) {
  const r = spawnSync(exe, args, { encoding: "utf8", timeout: 30000 });
  console.log(label, "| error:", r.error ? r.error.code : "none", "| status:", r.status,
              "| out:", (r.stdout || "").trim().split("\n").pop().slice(0, 50));
}
