import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/query-result'
const LEDGER_ENDPOINT = '/api/query-field-ledger'

type RunRow = {
  run_id: string
  "定义编号"?: string
  status?: string
  "参数快照"?: Record<string, unknown>
  "结果字段"?: string[]
  "排序规则"?: string
  "记录数"?: number
  "创建时间"?: string
}

type ResultPage = {
  items: Record<string, unknown>[]
  total: number
  page: number
  size: number
  run_id?: string
  "参数快照"?: Record<string, unknown>
  "结果字段"?: string[]
  "排序规则"?: string
  记录编号?: string[]
}

type LedgerRow = {
  台账编号?: string
  字段序号?: number
  字段名称?: string
  字段类型?: string
  字段来源?: string
}

const sortOptions = ["创建时间", "定义编号", "运行编号", "记录数"]
const statusOptions = ["已完成", "失败"]

export default function QueryResultView() {
  const [rows, setRows] = useState<RunRow[]>([])
  const [total, setTotal] = useState(0)
  const [snapshot, setSnapshot] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(5)
  const [filters, setFilters] = useState({ definition_no: '', status: '', sort: '创建时间', order: 'desc' })
  const [errorMessage, setErrorMessage] = useState('')

  const [activeRun, setActiveRun] = useState<RunRow | null>(null)
  const [records, setRecords] = useState<ResultPage | null>(null)
  const [recordPage, setRecordPage] = useState(1)
  const [ledger, setLedger] = useState<LedgerRow[]>([])
  const [verification, setVerification] = useState<Record<string, unknown> | null>(null)
  const [notice, setNotice] = useState('')

  const reload = useCallback(async (targetPage: number, keepSnapshot: boolean) => {
    setErrorMessage('')
    const query = new URLSearchParams({
      page: String(targetPage),
      size: String(pageSize),
      sort: filters.sort,
      order: filters.order,
    })
    if (filters.definition_no) {
      query.set('definition_no', filters.definition_no)
    }
    if (filters.status) {
      query.set('status', filters.status)
    }
    if (keepSnapshot && snapshot !== null) {
      // 回传首次响应的 snapshot：翻页期间新增的执行不会把后面的页往后挤。
      query.set('snapshot', String(snapshot))
    }
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('查询结果清单读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setSnapshot(payload.snapshot ?? null)
      setPage(payload.page ?? targetPage)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询结果清单读取失败')
    }
  }, [filters, pageSize, snapshot])

  useEffect(() => {
    setSnapshot(null)
    void reload(1, false)
    // 过滤条件变化时重新定位到第一页，并重新取一份翻页快照。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.definition_no, filters.status, filters.sort, filters.order])

  async function openRun(row: RunRow, targetPage = 1) {
    setErrorMessage('')
    setNotice('')
    setVerification(null)
    try {
      const response = await request(`${ENDPOINT}/${row.run_id}/page?page=${targetPage}`)
      if (!response.ok) {
        throw new Error('查询结果分页读取失败')
      }
      const payload = (await response.json()) as ResultPage
      setActiveRun(row)
      setRecords(payload)
      setRecordPage(payload.page ?? targetPage)

      const ledgerResponse = await request(`${LEDGER_ENDPOINT}?run_id=${encodeURIComponent(row.run_id)}`)
      if (ledgerResponse.ok) {
        const ledgerPayload = await ledgerResponse.json()
        setLedger(ledgerPayload.items ?? [])
      } else {
        setLedger([])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询结果读取失败')
    }
  }

  async function verify(run: RunRow) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${run.run_id}/verify`, { method: 'POST' })
      if (!response.ok) {
        throw new Error('复核读取失败')
      }
      setVerification(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '复核失败')
    }
  }

  const fields = records?.['结果字段'] ?? []
  const pageCount = records ? Math.max(1, Math.ceil((records.total ?? 0) / (records.size ?? 1))) : 1

  return (
    <section className="page" data-module="query-result">
      <header className="page-head">
        <div>
          <h2>查询结果管理</h2>
          <p className="page-desc">
            按查询定义复核结果字段与分页记录：每次执行冻结参数快照、结果字段与排序规则，
            连续翻页只认同一份快照，重新执行后才采用新口径。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出查询结果清单
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${LEDGER_ENDPOINT}/export`, '_blank')}>
            导出查询字段台账
          </button>
        </div>
      </header>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          setSnapshot(null)
          void reload(1, false)
        }}
      >
        <label className="filter-item">
          <span>定义编号</span>
          <input
            value={filters.definition_no}
            placeholder="按定义编号检索，如 QUER-0003"
            onChange={(event) => setFilters((prev) => ({ ...prev, definition_no: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部</option>
            {statusOptions.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>排序字段</span>
          <select
            value={filters.sort}
            onChange={(event) => setFilters((prev) => ({ ...prev, sort: event.target.value }))}
          >
            {sortOptions.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>排序方向</span>
          <select
            value={filters.order}
            onChange={(event) => setFilters((prev) => ({ ...prev, order: event.target.value }))}
          >
            <option value="desc">降序</option>
            <option value="asc">升序</option>
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button
          className="btn ghost"
          type="button"
          onClick={() => setFilters({ definition_no: '', status: '', sort: '创建时间', order: 'desc' })}
        >
          重置条件
        </button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>运行编号</th>
            <th>定义编号</th>
            <th>状态</th>
            <th>参数快照</th>
            <th>结果字段</th>
            <th>排序规则</th>
            <th>记录数</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.run_id}>
              <td>{row.run_id}</td>
              <td>{row['定义编号'] ?? '—'}</td>
              <td>{row.status ?? '—'}</td>
              <td>{JSON.stringify(row['参数快照'] ?? {})}</td>
              <td>{(row['结果字段'] ?? []).join('、') || '—'}</td>
              <td>{row['排序规则'] ?? '—'}</td>
              <td>{row['记录数'] ?? 0}</td>
              <td>{row['创建时间'] ?? '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openRun(row, 1)}>查看结果</button>
                <button className="link" type="button" onClick={() => void verify(row)}>按定义复核</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={9}>还没有查询结果，可先在数据查询页执行查询</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>
          共 {total} 条查询结果 · 第 {page} 页
          {snapshot !== null ? ` · 翻页快照 #${snapshot}` : ''}
        </span>
        <span className="row-actions">
          <button className="link" type="button" disabled={page <= 1} onClick={() => void reload(page - 1, true)}>
            上一页
          </button>
          <button
            className="link"
            type="button"
            disabled={page * pageSize >= total}
            onClick={() => void reload(page + 1, true)}
          >
            下一页
          </button>
        </span>
      </footer>

      {activeRun && records && (
        <section className="result-panel">
          <header className="page-head">
            <div>
              <h3>结果记录：{activeRun.run_id}</h3>
              <p className="page-desc">
                参数快照 {JSON.stringify(records['参数快照'] ?? {})} · 排序 {records['排序规则'] ?? '—'} ·
                字段 {(records['结果字段'] ?? []).join('、') || '—'}
              </p>
            </div>
          </header>
          <table className="data-table">
            <thead>
              <tr>
                {fields.map((field) => <th key={field}>{field}</th>)}
                <th>记录编号</th>
              </tr>
            </thead>
            <tbody>
              {records.items.map((record, index) => (
                <tr key={String((records['记录编号'] ?? [])[index] ?? index)}>
                  {fields.map((field) => (
                    <td key={field}>{record[field] === null || record[field] === undefined ? '—' : String(record[field])}</td>
                  ))}
                  <td>{String((records['记录编号'] ?? [])[index] ?? '—')}</td>
                </tr>
              ))}
              {records.items.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={fields.length + 1}>没有符合参数的结果记录</td>
                </tr>
              )}
            </tbody>
          </table>
          <footer className="page-foot">
            <span>第 {recordPage} 页 / 共 {records.total} 条</span>
            <span className="row-actions">
              <button className="link" type="button" disabled={recordPage <= 1} onClick={() => void openRun(activeRun, recordPage - 1)}>
                上一页
              </button>
              <button
                className="link"
                type="button"
                disabled={recordPage >= pageCount}
                onClick={() => void openRun(activeRun, recordPage + 1)}
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
        </section>
      )}

      {verification && (
        <section className="result-panel">
          <strong className={verification.ok ? '' : 'error-text'}>
            {String(verification['复核结论'] ?? '复核完成')}
          </strong>
          <p className="page-desc">
            分页记录一致：{verification['分页记录一致'] ? '是' : '否'} ·
            字段台账一致：{verification['字段台账一致'] ? '是' : '否'} ·
            共 {String(verification['分页数'] ?? 0)} 页
          </p>
          {(verification['口径漂移'] as string[] | undefined)?.length ? (
            <ul className="error-text">
              {(verification['口径漂移'] as string[]).map((item) => <li key={item}>{item}</li>)}
            </ul>
          ) : null}
        </section>
      )}

      <footer className="page-foot">
        {notice && <span className="page-desc">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
