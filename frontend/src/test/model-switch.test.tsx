import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { api } from '../api/client';
import { snapshotSchema } from '../api/contracts';
import { ProgressView } from '../features/run-progress/ProgressView';

afterEach(() => vi.restoreAllMocks());

it.each([true, false])('模型切换须点击确认，决定=%s', async (allowed) => {
  const snapshot = snapshotSchema.parse({
    task_id: 'task', status: 'running', created_at: 'now', updated_at: 'now', error: null,
    model_approval: { request_id: 'request', source: 'local-qwen2.5-7b', target: 'deepseek-flash',
      target_label: '硅基流动 API', reason: 'model.transport', message: '是否允许切换？可能产生费用。' },
  });
  const decide = vi.spyOn(api, 'decideModelSwitch').mockResolvedValue(snapshot);
  render(<ProgressView snapshot={snapshot} reconnecting={false}/>);
  expect(screen.getByRole('alertdialog')).toHaveTextContent('是否允许切换？');
  expect(decide).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: allowed ? '允许切换到硅基流动 API' : '拒绝并中断' }));
  expect(decide).toHaveBeenCalledWith('task', 'request', allowed);
  expect(screen.getByRole('button', { name: '拒绝并中断' })).toBeDisabled();
});
