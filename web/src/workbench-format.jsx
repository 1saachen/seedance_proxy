export const statusNames = {
  active: '有效',
  disabled: '已撤销',
  revoked: '已撤销',
  queued: '排队中',
  running: '运行中',
  pending: '待处理',
  submitting: '提交中',
  submission_unknown: '提交待确认',
  submission_failed: '提交失败',
  succeeded: '成功',
  completed: '成功',
  failed: '失败',
  canceled: '已取消',
  cancelled: '已取消',
  expired: '已过期',
  unknown: '状态待确认',
};

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  dateStyle: 'medium',
  timeStyle: 'short',
});
const numberFormatter = new Intl.NumberFormat('zh-CN');

export function formatDate(value) {
  if (value == null) return '—';
  let timestamp = value;
  if (typeof value === 'number' && value < 10 ** 11) timestamp = value * 1000;
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(value)) {
    timestamp = `${value.replace(' ', 'T')}Z`;
  }
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? '—' : dateFormatter.format(date);
}

export function formatNumber(value) {
  return value == null ? '—' : numberFormatter.format(value);
}

export function Status({ value }) {
  const tone = ['active', 'succeeded', 'completed'].includes(value)
    ? 'positive'
    : ['failed', 'submission_failed'].includes(value)
      ? 'negative'
      : 'neutral';
  return (
    <span className={`status ${tone}`}>
      {Object.hasOwn(statusNames, value) ? statusNames[value] : value || '未知'}
    </span>
  );
}
