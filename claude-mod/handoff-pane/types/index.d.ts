/** Where one summary stands: `working` while a model call runs, `error` with the reason. */
export type Status = 'idle' | 'working' | 'ready' | 'error'

/** A summary in two lengths: `short` for the default view, `detail` for "More detail". */
export type Summary = {
  status: Status
  short: string
  detail: string
  error?: string
  updatedAt?: number
}

export type Check = {
  id: string
  outcome: string
  detail?: string
  reason?: string
  command?: string
  excerpt?: string[] | null
}

export type Finding = { evidence: string; text: string }

/** The last report the verifier saved (`.git/task-handoff/last_report.json`), the fields drawn. */
export type Report = {
  generated_at?: string
  verdict: string
  task?: string | null
  checks: Check[]
  findings: Finding[]
  risks: string[]
  next_prompt?: string
}

export type Change = { path: string; status: string; added: number; removed: number; note?: string }

/** What `task-handoff pane <repo>` prints. */
export type PaneData = {
  repo: string
  name: string
  state_dir: string
  baseline: { task?: string | null; created_at?: string | null; head?: string | null; is_session: boolean } | null
  changes: Change[]
  pre_existing_unrelated: string[]
  diff: string
  diff_omitted: string[]
  report: Report | null
  stale: boolean
}

export type Verification = {
  status: Status
  data?: PaneData
  error?: string
  updatedAt?: number
}

declare module 'claude-code' {
  interface PluginState {
    'handoff-pane': {
      isExpanded: boolean
      project: Summary
      session: Summary
      verification: Verification
    }
  }
}
