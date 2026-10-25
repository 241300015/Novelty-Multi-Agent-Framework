import { Check, Clock3 } from 'lucide-react';
import { useState } from 'react';
import { api, normalizeError } from '../../api/client';
import { stageIndex, stageLabels, type RunSnapshot } from '../../api/contracts';
export function ProgressView({ snapshot, reconnecting }: { snapshot: RunSnapshot | null; reconnecting: boolean }) {
  const current = snapshot?.progress ? stageIndex(snapshot.progress.stage) : -1;
  const [sending, setSending] = useState(false);
  const [answered, setAnswered] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const approval = snapshot?.model_approval;
  const decide = async (allowed: boolean) => {
    if (!snapshot || !approval) return;
    setSending(true); setError(null);
    try { await api.decideModelSwitch(snapshot.task_id, approval.request_id, allowed); setAnswered(approval.request_id); }
    catch (cause) { setError(normalizeError(cause).message); }
    finally { setSending(false); }
  };
  return <section className="panel progress-view" aria-labelledby="progress-title">
    <p className="eyebrow">查新进行中</p>
    <h1 id="progress-title">{snapshot?.status === 'queued' ? '任务排队中' : snapshot ? '正在查新' : '正在恢复任务'}</h1>
    {approval && <div role="alertdialog" aria-labelledby="model-switch-title" aria-describedby="model-switch-message">
      <h2 id="model-switch-title">等待模型切换确认</h2>
      <p id="model-switch-message">{approval.message}</p>
      <button type="button" disabled={sending || answered === approval.request_id} onClick={() => void decide(true)}>允许切换到{approval.target_label}</button>
      <button type="button" disabled={sending || answered === approval.request_id} onClick={() => void decide(false)}>拒绝并中断</button>
      {answered === approval.request_id && <p role="status">已提交决定，等待任务状态更新。</p>}
      {error && <p role="alert">{error}</p>}
    </div>}
    {current >= 0 && snapshot?.status === 'running' ? <>
      <div className="lyrics-window"><ol className="lyrics" style={{ transform: `translateY(${112 - current * 56}px)` }}>
        {stageLabels.map((label, index) => <li key={label} className={index === current ? 'current' : index < current ? 'done' : ''} aria-current={index === current ? 'step' : undefined}>{index < current ? <Check size={18}/> : <span className="step-number">{String(index + 1).padStart(2, '0')}</span>}{label}</li>)}
      </ol></div>
      <p role="status">第 {current + 1} / 6 阶段{(snapshot.progress?.round ?? 0) > 1 && current >= 2 && current <= 4 ? ` · 正在补充检索 · 第 ${snapshot.progress?.round} 轮` : ''}</p>
    </> : <div className="indeterminate"><Clock3 size={34}/><p>任务状态将自动更新</p></div>}
    <p className="connection" role="status">{reconnecting ? '连接中断，正在重连' : '离开页面后，任务仍会继续运行'}</p>
    {snapshot && <p className="task-id">任务编号 {snapshot.task_id}</p>}
  </section>;
}
