import { describe, it, expect } from 'vitest'
import Database from 'better-sqlite3'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  collectModelSuggestInputs,
  configRootFor,
  countMcpServers,
  type ModelSuggestInputDeps,
} from '../web/model-suggest-inputs.js'
import { suggestForAgent, CONTEXT_PER_CALL_HIGH } from '../web/model-suggest.js'
import { MODEL_SUGGEST_KANBAN_SQL } from '../db.js'
import { MAIN_AGENT_ID } from '../config.js'
import { DISTRIBUTION_DEFAULT_AGENT_MODEL } from '../config-registry.js'

// MODELSUGGEST909. On 2026-09-07 the dashboard advised downgrading the MAIN
// agent from Opus to Sonnet and signed it off with "minden szempont adattal
// alátámasztott". Three of five inputs were wrong and all three shared one
// cause: the endpoint addressed the main agent through agents/<name>/, which
// does not exist for it. The existing model-suggest tests all used sub-agent
// names, so none of them could see it -- that is the gap this file closes.

const ROOT = '/fake/project'

function fakeDeps(files: Record<string, string>, ctx: Record<string, number> = {}) {
  const asked: string[] = []
  const deps: ModelSuggestInputDeps = {
    readFile(path) { asked.push(path); return files[path] ?? null },
    readContextTokens(workingDir, configDir) {
      asked.push(`ctx:${workingDir}:${configDir ?? ''}`)
      return ctx[workingDir] ?? null
    },
    configDirFor() { return undefined },
    projectRoot: ROOT,
    homeDir: '/fake/home',
  }
  return { deps, asked }
}

describe('model-suggest inputs for the MAIN agent', () => {
  it('reads the persona from the project root, not from agents/<name>', () => {
    const { deps, asked } = fakeDeps({
      [join(ROOT, 'CLAUDE.md')]: 'Koordinálod a flottát, komplex döntések.',
      [join(ROOT, 'SOUL.md')]: 'Melankolikus asszisztens.',
    })
    const inputs = collectModelSuggestInputs(MAIN_AGENT_ID, deps)

    expect(inputs.personaText).toContain('Koordinálod')
    // SOUL.md is half the persona; classifyPersona's contract says CLAUDE.md +
    // SOUL.md, and the old call site read CLAUDE.md alone.
    expect(inputs.personaText).toContain('Melankolikus')
    expect(configRootFor(MAIN_AGENT_ID, ROOT)).toBe(ROOT)
    // The bug, pinned directly: nothing may be looked for under agents/marveen.
    expect(asked.some(p => p.includes(join('agents', MAIN_AGENT_ID)))).toBe(false)
  })

  it('counts the main agent MCP servers from the config project scope (no .mcp.json exists)', () => {
    const { deps } = fakeDeps({
      [join(ROOT, '.channels-config', '.claude.json')]: JSON.stringify({
        projects: { [ROOT]: { mcpServers: { gmail: {}, 'google-calendar': {} } } },
      }),
    })
    expect(countMcpServers(MAIN_AGENT_ID, deps)).toBe(2)
  })

  it('a sub-agent still counts its own .mcp.json, and an empty one is 0 -- not a fallback', () => {
    const subRoot = configRootFor('rita', ROOT)
    const { deps } = fakeDeps({
      [join(subRoot, '.mcp.json')]: JSON.stringify({ mcpServers: { a: {}, b: {}, c: {} } }),
      // present, and deliberately NOT counted for a sub-agent:
      [join(ROOT, '.channels-config', '.claude.json')]: JSON.stringify({
        projects: { [ROOT]: { mcpServers: { gmail: {}, 'google-calendar': {} } } },
      }),
    })
    expect(countMcpServers('rita', deps)).toBe(3)

    const empty = fakeDeps({ [join(subRoot, '.mcp.json')]: JSON.stringify({ mcpServers: {} }) })
    expect(countMcpServers('rita', empty.deps)).toBe(0)
  })

  it('reads context tokens from the project root, so the >150K override can actually fire', () => {
    const { deps } = fakeDeps(
      { [join(ROOT, 'CLAUDE.md')]: 'Általános.' },
      { [ROOT]: 220_000 },
    )
    const inputs = collectModelSuggestInputs(MAIN_AGENT_ID, deps)
    expect(inputs.contextTokens).toBe(220_000)

    const suggestion = suggestForAgent(
      MAIN_AGENT_ID, DISTRIBUTION_DEFAULT_AGENT_MODEL, inputs.personaText, inputs.contextTokens,
    )
    // The override is the rule that keeps a heavy agent on the top tier. With
    // contextTokens structurally stuck at 0 it never fired for the main agent.
    expect(suggestion.suggestedModel).toBe(DISTRIBUTION_DEFAULT_AGENT_MODEL)
    expect(suggestion.changeAdvised).toBe(false)
  })

  it('the measured main-agent signal set no longer produces a downgrade', () => {
    // Real numbers, 30 days to 2026-09-09: 224K context/call (input_tokens alone
    // was 6/call), 2 MCP servers, 2 high-priority open cards.
    const result = suggestForAgent(
      MAIN_AGENT_ID,
      DISTRIBUTION_DEFAULT_AGENT_MODEL,
      'Gábor személyes AI asszisztense vagy. Koordinálod a flottát, komplex, többlépéses feladatok, döntések.',
      0,
      {
        tokenAvgContextPerCall: 224_000,
        kanbanOpenCount: 17,
        kanbanUrgentCount: 2,
        scheduledFreqPerDay: 30,
        mcpServerCount: 2,
      },
    )
    expect(result.suggestedModel).toBe(DISTRIBUTION_DEFAULT_AGENT_MODEL)
    expect(result.changeAdvised).toBe(false)
    expect(result.reason).toMatch(/224\.0K kontextus-token\/hívás/)
  })

  it('the same set read the OLD way (input_tokens only) is what produced the downgrade', () => {
    // 6 tokens/call: the cache-miss sliver. Kept as an executable record of the
    // failure -- this is the input that scored "alacsony" for the heaviest agent.
    const result = suggestForAgent(
      MAIN_AGENT_ID, DISTRIBUTION_DEFAULT_AGENT_MODEL, 'Általános asszisztens.', 0,
      { tokenAvgContextPerCall: 6, kanbanOpenCount: 8, kanbanUrgentCount: 0, scheduledFreqPerDay: 30, mcpServerCount: 0 },
    )
    expect(result.suggestedModel).toBe('claude-sonnet-5')
    expect(result.changeAdvised).toBe(true)
    expect(6).toBeLessThan(CONTEXT_PER_CALL_HIGH)
  })
})

describe('MODEL_SUGGEST_KANBAN_SQL counts live work only', () => {
  it('excludes done and archived cards from the per-assignee load', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ms-kanban-'))
    const db = new Database(join(dir, 'test.db'))
    try {
      db.exec(`CREATE TABLE kanban_cards (
        id TEXT PRIMARY KEY, title TEXT, status TEXT, priority TEXT,
        assignee TEXT, archived_at INTEGER, updated_at INTEGER, created_at INTEGER, sort_order INTEGER
      )`)
      const ins = db.prepare(
        'INSERT INTO kanban_cards (id,title,status,priority,assignee,archived_at,updated_at,created_at,sort_order) VALUES (?,?,?,?,?,?,?,?,0)',
      )
      ins.run('A', 'live high', 'waiting', 'high', 'marveen', null, 1, 1)
      ins.run('B', 'live high 2', 'planned', 'high', 'marveen', null, 1, 1)
      ins.run('C', 'finished high', 'done', 'high', 'marveen', null, 1, 1)
      ins.run('D', 'finished normal', 'done', 'normal', 'marveen', null, 1, 1)
      ins.run('E', 'archived high', 'waiting', 'high', 'marveen', 12345, 1, 1)
      ins.run('F', 'unassigned', 'waiting', 'high', null, null, 1, 1)

      const rows = db.prepare(MODEL_SUGGEST_KANBAN_SQL).all() as
        { assignee: string; priority: string; cnt: number }[]
      const open = rows.reduce((n, r) => n + r.cnt, 0)
      const urgent = rows
        .filter(r => r.priority === 'urgent' || r.priority === 'high')
        .reduce((n, r) => n + r.cnt, 0)

      // 2, not 4: the two done cards used to be counted as "aktív kártya".
      expect(open).toBe(2)
      expect(urgent).toBe(2)
    } finally {
      db.close()
      rmSync(dir, { recursive: true, force: true })
    }
  })
})
