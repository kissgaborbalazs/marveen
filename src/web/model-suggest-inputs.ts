// Per-agent INPUT collection for the model-suggest endpoint.
//
// Split out of routes/agents.ts for one reason: the route's own I/O was the
// broken half of the feature, and a handler that reads the filesystem inline
// cannot be unit-tested. Everything here takes an injectable dependency set, so
// the main-agent path is pinned by a test (model-suggest-main-agent.test.ts)
// instead of by a live endpoint someone has to remember to eyeball.
//
// MODELSUGGEST909, measured 2026-09-07 on the live endpoint: the dashboard
// advised downgrading the MAIN agent from claude-opus-5[1m] to claude-sonnet-5,
// with the closing line "Bizonytalanság: minden szempont adattal alátámasztott".
// Three of the five inputs behind that verdict were wrong, and all three failed
// the SAME way: the route addressed the main agent through agentDir(name), i.e.
// `agents/marveen/`, a directory that does not exist. The main agent's persona,
// MCP config and transcripts live in PROJECT_ROOT. Empty persona -> 0 keyword
// hits ("Általános persona"), missing .mcp.json -> 0 servers ("minimális
// integráció"), missing project dir -> contextTokens 0, which structurally
// disarmed the >150K context override -- the one rule that exists to keep a
// heavy agent on the top tier. The suggestion was not a finding, it was the
// default fallback at the end of suggestForAgent.
//
// The correct root function already existed (agentConfigRoot: PROJECT_ROOT for
// the main agent, agents/<name> for sub-agents) and was used elsewhere in the
// same file; this call site simply never adopted it.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { PROJECT_ROOT } from '../config.js'
import { agentDir } from './agent-config.js'
import { readContextTokensFromProjectDir } from './active-model.js'
import { resolveAgentConfigDir } from './claude-plans.js'
import { isMainChannelsAgent } from './main-agent.js'

export interface ModelSuggestInputs {
  /** CLAUDE.md + SOUL.md + personas/<name>.md, concatenated */
  personaText: string
  /** declared MCP servers (sub-agent .mcp.json, or the main agent's config scope) */
  mcpServerCount: number
  /** live session context size in tokens, 0 when not measurable */
  contextTokens: number
}

export interface ModelSuggestInputDeps {
  readFile(path: string): string | null
  readContextTokens(workingDir: string, configDir?: string): number | null
  configDirFor(name: string): string | undefined
  projectRoot: string
  homeDir: string
}

export const defaultInputDeps: ModelSuggestInputDeps = {
  readFile(path: string): string | null {
    try { return existsSync(path) ? readFileSync(path, 'utf-8') : null } catch { return null }
  },
  readContextTokens: readContextTokensFromProjectDir,
  // Same rule the conversation viewer uses (routes/agent-conversation.ts): the
  // main agent's transcripts are read through the host login, sub-agents through
  // their resolved plan/config dir. For this install the two are the same files
  // anyway -- .channels-config/projects is a symlink to ~/.claude/projects --
  // but the rule, not the symlink, is what makes it correct on other installs.
  configDirFor(name: string): string | undefined {
    if (isMainChannelsAgent(name)) return undefined
    return resolveAgentConfigDir(name).configDir ?? undefined
  },
  projectRoot: PROJECT_ROOT,
  homeDir: homedir(),
}

/**
 * Where an agent's persona, MCP config and transcripts actually live.
 * PROJECT_ROOT for the main agent, agents/<name> for everyone else.
 */
export function configRootFor(name: string, projectRoot = PROJECT_ROOT): string {
  return isMainChannelsAgent(name) ? projectRoot : agentDir(name)
}

/**
 * Declared MCP servers.
 *
 * Sub-agents get an `agents/<name>/.mcp.json` from the scaffolder, so that file
 * is the answer whenever it exists. The main agent has none: its servers are
 * registered with `claude mcp add -s local`, which writes them into the PROJECT
 * SCOPE of a `.claude.json` (`projects[<cwd>].mcpServers`) -- for this install
 * `.channels-config/.claude.json`, holding gmail + google-calendar. Counting
 * only `.mcp.json` reported 0 servers for an agent that runs two of them.
 *
 * Union across the candidate config files rather than first-hit: which of the
 * two holds the local scope depends on MAIN_AGENT_ISOLATED_CONFIG, and an agent
 * is no less integrated because its servers are split across both.
 */
export function countMcpServers(name: string, deps: ModelSuggestInputDeps): number {
  const explicit = deps.readFile(join(configRootFor(name, deps.projectRoot), '.mcp.json'))
  if (explicit !== null) {
    try {
      const cfg = JSON.parse(explicit) as { mcpServers?: Record<string, unknown> }
      return Object.keys(cfg.mcpServers ?? {}).length
    } catch { return 0 }
  }

  const names = new Set<string>()
  const candidates = [
    join(deps.projectRoot, '.channels-config', '.claude.json'),
    join(deps.homeDir, '.claude.json'),
  ]
  for (const path of candidates) {
    const raw = deps.readFile(path)
    if (raw === null) continue
    try {
      const cfg = JSON.parse(raw) as {
        mcpServers?: Record<string, unknown>
        projects?: Record<string, { mcpServers?: Record<string, unknown> }>
      }
      const scoped = cfg.projects?.[deps.projectRoot]?.mcpServers ?? {}
      for (const key of Object.keys(scoped)) names.add(key)
      for (const key of Object.keys(cfg.mcpServers ?? {})) names.add(key)
    } catch { /* a malformed config counts as no servers, not as a crash */ }
  }
  return names.size
}

/**
 * Persona text the classifier scores.
 *
 * CLAUDE.md + SOUL.md is what classifyPersona's own contract says it receives,
 * and what getAgentDetail reads; the model-suggest path used to read CLAUDE.md
 * alone and thereby scored half the persona.
 */
export function readPersonaText(name: string, deps: ModelSuggestInputDeps): string {
  const root = configRootFor(name, deps.projectRoot)
  return [
    deps.readFile(join(root, 'CLAUDE.md')),
    deps.readFile(join(root, 'SOUL.md')),
    deps.readFile(join(deps.projectRoot, 'personas', `${name}.md`)),
  ].filter((v): v is string => typeof v === 'string' && v.length > 0).join('\n')
}

export function collectModelSuggestInputs(
  name: string,
  deps: ModelSuggestInputDeps = defaultInputDeps,
): ModelSuggestInputs {
  const root = configRootFor(name, deps.projectRoot)
  return {
    personaText: readPersonaText(name, deps),
    mcpServerCount: countMcpServers(name, deps),
    contextTokens: deps.readContextTokens(root, deps.configDirFor(name)) ?? 0,
  }
}
