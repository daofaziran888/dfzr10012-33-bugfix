import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/udf'
const columns = [
  "函数编号",
  "函数名称",
  "输入参数",
  "返回类型",
  "函数代码",
  "测试用例",
  "注册时间",
  "函数状态"
]
const actions = [
  "注册函数",
  "校验引用",
  "停用函数",
  "删除函数"
]
const statuses = [
  "草稿",
  "已注册",
  "已停用",
  "已删除"
]

type Row = Record<string, string | number | null>
type ReferenceResult = Record<string, unknown>

type ActionResultPayload = {
  ok: boolean
  message: string
}

export default function UdfView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [references, setReferences] = useState<ReferenceResult[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const stats = useMemo(() => {
    const count = (status: string) => rows.filter((row) => row.status === status).length
    return [
      { label: '函数定义总数', value: total },
      { label: '已注册函数', value: count('已注册') },
      { label: '已停用函数', value: count('已停用') },
    ]
  }, [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('函数定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      const referenceResponse = await request(`${ENDPOINT}/references?size=50`)
      if (referenceResponse.ok) {
        const referencePayload = await referenceResponse.json()
        setReferences(referencePayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '自定义函数库列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('自定义函数库动作未生效，请稍后重试')
      }
      const payload = (await response.json()) as ActionResultPayload
      if (!payload.ok) {
        // 业务拒绝（例如重复停用、缺字段、版本冲突）以 ok=false 返回，原样提示原因。
        setErrorMessage(payload.message)
        return
      }
      setNoticeMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '自定义函数库操作失败')
    }
  }

  return (
    <section className="page" data-module="udf">
      <header className="page-head">
        <div>
          <h2>自定义函数库管理</h2>
          <p className="page-desc">维护自定义函数的参数定义、代码实现与测试验证，停用前先完成引用索引匹配，停用结果保留匹配算子与测试用例。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('函数定义登记入口尚未接入审批流')}>
            登记函数定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出自定义函数库清单
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
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无自定义函数库数据，可先登记函数定义</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">函数引用数据结果</h3>
      <table className="data-table" data-module="udf-reference">
        <thead>
          <tr>
            <th>记录编号</th>
            <th>函数编号</th>
            <th>匹配算子</th>
            <th>停用状态</th>
            <th>测试用例</th>
            <th>版本</th>
            <th>确认状态</th>
            <th>采用算子</th>
          </tr>
        </thead>
        <tbody>
          {references.map((reference) => (
            <tr key={String(reference['记录编号'])}>
              <td>{String(reference['记录编号'] ?? '—')}</td>
              <td>{String(reference['函数编号'] ?? '—')}</td>
              <td>{Array.isArray(reference['匹配算子']) ? (reference['匹配算子'] as string[]).join('、') || '—' : '—'}</td>
              <td>{String(reference['停用状态'] ?? '—')}</td>
              <td>{String(reference['测试用例'] ?? '—')}</td>
              <td>{String(reference['版本'] ?? '—')}</td>
              <td>{String(reference['确认状态'] ?? '—')}</td>
              <td>{Array.isArray(reference['采用算子']) ? (reference['采用算子'] as string[]).join('、') || '—' : '—'}</td>
            </tr>
          ))}
          {references.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={8}>暂无引用数据结果，停用或校验引用后会在这里生成</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条自定义函数库记录，{references.length} 条引用数据结果</span>
        {noticeMessage && <span>{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
