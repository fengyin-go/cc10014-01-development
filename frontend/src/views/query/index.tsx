import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/query'
const RESULT_ENDPOINT = '/api/query-result'
const LEDGER_ENDPOINT = '/api/query-field-ledger'
const columns = [
  "定义编号",
  "关联数据源",
  "查询模板",
  "参数占位",
  "结果字段",
  "排序规则",
  "分页大小",
  "超时秒数"
]
const actions = [
  "提交校验",
  "启用查询",
  "停用查询"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "查询定义总数",
    "value": 0
  },
  {
    "label": "已启用定义",
    "value": 0
  },
  {
    "label": "待校验定义",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type AnyMap = Record<string, unknown>

type ResultRecord = { 记录编号?: string; [key: string]: unknown }
type ResultPage = {
  items?: ResultRecord[]
  total?: number
  page?: number
  size?: number
  run_id?: string
  "定义编号"?: string
  "参数快照"?: AnyMap
  "结果字段"?: string[]
  "排序规则"?: string
  记录编号?: string[]
  源记录编号?: unknown[]
  参数版本?: number
  排序版本?: number
  字段版本?: number
  快照锁定?: boolean
  口径仲裁?: Record<string, string[]>
}
type VerifyResult = {
  ok?: boolean
  复核结论?: string
  口径漂移?: string[]
  分页记录一致?: boolean
  字段台账一致?: boolean
  重复记录编号?: string[]
  分页数?: number
}
type LedgerItem = {
  台账编号?: string
  字段序号?: number
  字段名称?: string
  字段类型?: string
  字段来源?: string
  字段版本?: number
  run_id?: string
}

function splitParams(value: unknown): Record<string, string> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        item === null || item === undefined ? '' : String(item),
      ]),
    )
  }
  const result: Record<string, string> = {}
  for (const item of String(value ?? '').split(/[,，;；\s]+/)) {
    const name = item.trim()
    if (name) {
      result[name] = ''
    }
  }
  return result
}

export default function QueryView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 查询结果状态：一次执行冻结参数快照与字段清单，翻页只认这份快照。
  const [activeRow, setActiveRow] = useState<Row | null>(null)
  const [params, setParams] = useState<Record<string, string>>({})
  const [paramDraft, setParamDraft] = useState<Record<string, string>>({})
  const [resultPage, setResultPage] = useState<ResultPage | null>(null)
  const [page, setPage] = useState(1)
  const [ledger, setLedger] = useState<LedgerItem[]>([])
  const [verification, setVerification] = useState<VerifyResult | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('查询定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据查询列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function readPage(runId: string, nextPage: number) {
    const query = new URLSearchParams({ page: String(nextPage) })
    const response = await request(`${RESULT_ENDPOINT}/${runId}/page?${query.toString()}`)
    if (!response.ok) {
      throw new Error('查询结果分页读取失败')
    }
    const payload = (await response.json()) as ResultPage
    setResultPage(payload)
    setPage(payload.page ?? nextPage)
    setVerification(null)

    const ledgerResponse = await request(`${LEDGER_ENDPOINT}?run_id=${encodeURIComponent(runId)}`)
    if (ledgerResponse.ok) {
      const ledgerPayload = await ledgerResponse.json()
      setLedger(ledgerPayload.items ?? [])
    } else {
      setLedger([])
    }
  }

  async function execute(row: Row, options: { force?: boolean; parameters?: Record<string, string> } = {}) {
    setErrorMessage('')
    setNotice('')
    const definition = String(row.定义编号 ?? '')
    const parameters = options.parameters ?? params
    try {
      const response = await request(`${RESULT_ENDPOINT}/execute`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            定义编号: definition,
            parameters,
            force: Boolean(options.force),
          },
        }),
      })
      if (!response.ok) {
        throw new Error('查询执行未生效，请稍后重试')
      }
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '查询执行失败')
      }
      setActiveRow(row)
      setParams(parameters)
      setParamDraft(parameters)
      setNotice(payload.message ?? '查询已执行')
      await readPage(payload.entry.run_id, 1)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询执行失败')
    }
  }

  async function verify() {
    if (!resultPage?.run_id) {
      return
    }
    setErrorMessage('')
    try {
      const response = await request(`${RESULT_ENDPOINT}/${resultPage.run_id}/verify`, { method: 'POST' })
      if (!response.ok) {
        throw new Error('复核读取失败')
      }
      setVerification((await response.json()) as VerifyResult)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '复核失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    if (action === '执行查询' || action === '重新执行') {
      await execute(row, { force: action === '重新执行' })
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('数据查询动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据查询操作失败')
    }
  }

  const availableParams = useMemo(
    () => splitParams(activeRow?.参数占位 ?? ''),
    [activeRow],
  )

  return (
    <section className="page" data-module="query">
      <header className="page-head">
        <div>
          <h2>数据查询管理</h2>
          <p className="page-desc">维护数据查询定义的输入、执行约束与结果记录，为数据查询提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('查询定义登记入口尚未接入审批流')}>
            登记查询定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据查询清单
          </button>
          <button className="btn" type="button" onClick={() => window.open('/api/query-result/export', '_blank')}>
            导出查询结果清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void runAction('执行查询', row)}>
                  执行查询
                </button>
                <button className="link" type="button" onClick={() => void runAction('重新执行', row)}>
                  重新执行
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据查询数据，可先登记查询定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {activeRow && (
        <section className="result-panel">
          <header className="page-head">
            <div>
              <h3>查询结果：{String(activeRow.定义编号 ?? '')}</h3>
              <p className="page-desc">
                {resultPage?.run_id
                  ? `运行 ${resultPage.run_id} · 参数快照 ${JSON.stringify(resultPage.参数快照 ?? {})} · 字段 ${(resultPage.结果字段 ?? []).join('、')}`
                  : '尚未执行'}
              </p>
            </div>
            <div className="page-actions">
              <button className="btn" type="button" onClick={() => void verify()}>按定义复核</button>
            </div>
          </header>

          <form
            className="filter-bar"
            onSubmit={(event) => {
              event.preventDefault()
              // 参数只在重新执行时生效，避免中途改变影响到正在翻页的快照。
              if (activeRow) {
                void execute(activeRow, { force: true, parameters: { ...paramDraft } })
              }
            }}
          >
            {Object.keys(availableParams).map((name) => (
              <label key={name} className="filter-item">
                <span>{name}</span>
                <input
                  value={paramDraft[name] ?? ''}
                  placeholder={`输入 ${name}`}
                  onChange={(event) => setParamDraft((prev) => ({ ...prev, [name]: event.target.value }))}
                />
              </label>
            ))}
            <button className="btn" type="submit">按新参数重新执行</button>
          </form>

          {resultPage && (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    {(resultPage.结果字段 ?? []).map((field) => <th key={field}>{field}</th>)}
                    <th>记录编号</th>
                  </tr>
                </thead>
                <tbody>
                  {(resultPage.items ?? []).map((record, index) => (
                    <tr key={String(record.记录编号 ?? index)}>
                      {(resultPage.结果字段 ?? []).map((field) => (
                        <td key={field}>{record[field] === null || record[field] === undefined ? '—' : String(record[field])}</td>
                      ))}
                      <td>{String((resultPage.记录编号 ?? [])[index] ?? '—')}</td>
                    </tr>
                  ))}
                  {(resultPage.items ?? []).length === 0 && (
                    <tr>
                      <td className="empty-state" colSpan={(resultPage.结果字段 ?? []).length + 1}>没有符合参数的结果记录</td>
                    </tr>
                  )}
                </tbody>
              </table>

              <footer className="page-foot">
                <span>
                  第 {page} 页 / 共 {resultPage.total ?? 0} 条 · 每页 {resultPage.size ?? 0} 条 ·
                  排序 {(resultPage.排序规则 ?? '—')} · 快照 {(resultPage.快照锁定 ? '已锁定' : '未锁定')}
                </span>
                <span className="row-actions">
                  <button className="link" type="button" disabled={page <= 1} onClick={() => void readPage(String(resultPage.run_id), page - 1)}>
                    上一页
                  </button>
                  <button
                    className="link"
                    type="button"
                    disabled={page * (resultPage.size ?? 1) >= (resultPage.total ?? 0)}
                    onClick={() => void readPage(String(resultPage.run_id), page + 1)}
                  >
                    下一页
                  </button>
                </span>
              </footer>

              {ledger.length > 0 && (
                <p className="page-desc">
                  查询字段台账：
                  {ledger.map((item) => `${item.字段序号 ?? ''}.${item.字段名称 ?? ''}(${item.字段类型 ?? '未知'})`).join(' · ')}
                </p>
              )}
            </>
          )}

          {verification && (
            <div className="verify-panel">
              <strong className={verification.ok ? '' : 'error-text'}>{verification.复核结论 ?? '复核完成'}</strong>
              <span className="page-desc">
                分页记录一致：{verification.分页记录一致 ? '是' : '否'} ·
                字段台账一致：{verification.字段台账一致 ? '是' : '否'} ·
                共 {verification.分页数 ?? 0} 页
              </span>
              {(verification.口径漂移 ?? []).length > 0 && (
                <ul className="error-text">
                  {(verification.口径漂移 ?? []).map((item) => <li key={item}>{item}</li>)}
                </ul>
              )}
              {(verification.重复记录编号 ?? []).length > 0 && (
                <p className="error-text">重复记录：{verification.重复记录编号?.join('、')}</p>
              )}
            </div>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据查询记录</span>
        {notice && <span className="page-desc">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
