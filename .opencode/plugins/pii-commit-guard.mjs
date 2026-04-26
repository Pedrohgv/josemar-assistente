import { execFileSync } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"

function isGitCommitCommand(command) {
  if (!command || typeof command !== "string") return false
  const compact = command.replace(/\s+/g, " ").trim().toLowerCase()
  return /(^|[;&|]\s*|&&\s*)git commit(\s|$)/.test(compact)
}

export const PiiCommitGuard = async ({ directory }) => {
  const root = directory

  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return

      const command = String(output?.args?.command || "")
      if (!isGitCommitCommand(command)) return

      const guardScript = path.join(root, "scripts", "pii_guard.py")
      if (!existsSync(guardScript)) {
        throw new Error("PII guard script not found at scripts/pii_guard.py")
      }

      try {
        execFileSync("python3", [guardScript, "--staged", "--fail-on", "medium"], {
          cwd: root,
          stdio: "inherit",
        })
      } catch {
        throw new Error("Commit blocked by deterministic PII guard (scripts/pii_guard.py)")
      }
    },
  }
}
