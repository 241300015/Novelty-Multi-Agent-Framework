import { reportResourceSchema, snapshotSchema } from './contracts';
export class ApiError extends Error {
  constructor(public code: 'network' | 'timeout' | 'http' | 'contract' | 'unavailable', message: string, public status?: number) { super(message); }
}
export function normalizeError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  if (error instanceof DOMException && error.name === 'TimeoutError') return new ApiError('timeout', '请求超时，请检查连接后重试。');
  return new ApiError('network', '网络连接中断，请检查网络后重试。');
}
export function safeApiUrl(path: string) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/novelty/')) throw new ApiError('contract', '报告地址不可用，请联系服务管理员。');
  return url.href;
}
export async function request(path: string, init: RequestInit = {}) {
  let response: Response;
  try {
    response = await fetch(safeApiUrl(path), { ...init, credentials: 'same-origin', signal: init.signal ? AbortSignal.any([init.signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000) });
  } catch (error) { throw normalizeError(error); }
  if (!response.ok) {
    const messages: Record<number, string> = { 404: '任务不存在，可能已因服务重启失效。请重新开始。', 413: '文件超出服务端限制，请缩小文件后重试。', 422: '文件或请求未通过校验，请检查后重新提交。', 401: '登录已失效，请重新登录后重试。', 403: '暂无权限访问此任务。' };
    throw new ApiError('http', messages[response.status] ?? '服务暂时不可用，请稍后重试。', response.status);
  }
  return response;
}
async function snapshot(response: Response) {
  try { return snapshotSchema.parse(await response.json()); }
  catch { throw new ApiError('contract', '服务响应格式不兼容，请联系服务管理员。'); }
}
export const api = {
  decideModelSwitch: async (id: string, requestId: string, allowed: boolean) => snapshot(await request(`/api/novelty/runs/${encodeURIComponent(id)}/model-switch`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: requestId, allowed }),
  })),
  getReportResource: async (id: string, signal: AbortSignal) => {
    const response = await request(`/api/novelty/report-artifacts/${encodeURIComponent(id)}`, { signal });
    try { return reportResourceSchema.parse(await response.json()); }
    catch { throw new ApiError('contract', '报告资源元数据格式不兼容。'); }
  },
  getRun: async (id: string, signal: AbortSignal) => snapshot(await request(`/api/novelty/runs/${encodeURIComponent(id)}`, { signal })),
  createRun: async (paper: File, signal: AbortSignal, submissionId: string) => {
    if (import.meta.env.VITE_FILE_API_ENABLED === 'false') throw new ApiError('unavailable', '当前服务尚未开放文件查新，请待服务升级后重试。');
    const body = new FormData(); body.append('paper', paper);
    return snapshot(await request('/api/novelty/runs/files', { method: 'POST', body, signal, headers: { 'X-Submission-Id': submissionId } }));
  },
};
export function downloadFilename(header: string | null) {
  const encoded = header?.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1];
  let filename = header?.match(/filename\s*=\s*"([^"]+)"/i)?.[1] ?? header?.match(/filename\s*=\s*([^;]+)/i)?.[1];
  if (encoded) { try { filename = decodeURIComponent(encoded); } catch { /* Fall back to the plain filename. */ } }
  // Never interpret a server-supplied filename as a path.
  const clean = filename?.replace(/[\\/\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, '').trim().replace(/^\.+/, '');
  if (!clean) throw new ApiError('contract', '报告缺少有效文件名，请联系服务管理员。');
  return clean;
}
export async function reportFile(path: string, signal: AbortSignal) {
  const response = await request(path, { signal });
  const type = response.headers.get('content-type')?.split(';')[0].trim();
  if (!type || !['text/markdown', 'text/plain', 'application/pdf'].includes(type)) throw new ApiError('contract', '暂不支持此报告格式。');
  const name = downloadFilename(response.headers.get('content-disposition'));
  return new File([await response.blob()], name, { type });
}
export function saveFile(file: File) {
  const url = URL.createObjectURL(file); const anchor = document.createElement('a');
  anchor.href = url; anchor.download = file.name; anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
