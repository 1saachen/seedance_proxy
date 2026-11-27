import { formatNumber } from './workbench-format.jsx';

const modelColors = ['#2448df', '#167e72', '#6654b9', '#b26724', '#2e7197'];

function yuan(amount) {
  return amount == null ? '—' : `¥${amount}`;
}

function scaleFor(days) {
  const largest = Math.max(0, ...days.map(({ amount }) => Number(amount)));
  if (!largest) return { limit: 0, ticks: days.some(({ amount }) => amount != null) ? ['0'] : [] };

  const targetStep = Math.max(largest / 4, 0.000001);
  const magnitude = 10 ** Math.floor(Math.log10(targetStep));
  const step = [1, 2, 5, 10].map((factor) => factor * magnitude).find((value) => value >= targetStep);
  const count = Math.ceil(largest / step);
  const ticks = Array.from({ length: count + 1 }, (_, index) =>
    (step * (count - index)).toLocaleString('zh-CN', { maximumFractionDigits: 6 }),
  );
  return { limit: step * count, ticks };
}

function modelColor(model) {
  if (typeof model !== 'string' || !model) return '#63778d';
  let hash = 0;
  for (const character of model) hash = (hash * 33 + character.codePointAt(0)) >>> 0;
  return modelColors[hash % modelColors.length];
}

function SpendDay({ day, limit }) {
  const { date, amount, models, pendingCalls, pricedCalls } = day;
  const dayLabel = `${Number(date.slice(5, 7))}/${Number(date.slice(8))}`;
  const amountLabel = amount == null ? '暂无已计价费用' : `已计价 ${yuan(amount)}`;
  const countLabel = `${formatNumber(pricedCalls)} 笔已计价${pendingCalls ? `，${formatNumber(pendingCalls)} 笔待计价` : ''}`;
  const breakdown = models.map(({ model, amount: modelAmount }) => `${model} ${yuan(modelAmount)}`);
  const details = [date, amountLabel, countLabel, ...breakdown].join('，');
  return (
    <a className="spend-day" href={`#billing?day=${date}`} aria-label={`${details}。查看调用记录`}>
      <span className="spend-track" aria-hidden="true">
        {limit > 0 &&
          models.map(
            ({ model, amount: part }) =>
              Number(part) > 0 && (
                <span
                  className="spend-segment"
                  key={model}
                  style={{ backgroundColor: modelColor(model), height: `${(Number(part) / limit) * 100}%` }}
                />
              ),
          )}
        {amount != null && Number(amount) === 0 && <span className="spend-zero" />}
        {amount == null && pendingCalls > 0 && <span className="spend-pending" />}
      </span>
      <time dateTime={date} aria-hidden="true">
        {dayLabel}
      </time>
      <span className="spend-tooltip" aria-hidden="true">
        <strong>{date}</strong>
        <span>{amountLabel}</span>
        <span>{countLabel}</span>
        {breakdown.map((label) => (
          <span key={label}>{label}</span>
        ))}
      </span>
    </a>
  );
}

export function SpendTrend({ trend }) {
  if (!trend?.days.length) return <p className="empty">暂无费用趋势数据。</p>;
  const { days, totalAmount } = trend;
  const { limit, ticks } = scaleFor(days);
  const models = [...new Set(days.flatMap((day) => day.models.map(({ model }) => model)))].sort();
  const pendingCalls = days.reduce((total, day) => total + day.pendingCalls, 0);
  return (
    <div className="spend-chart">
      <div className="spend-heading">
        <p className="spend-total">{totalAmount == null ? '暂无已计价费用' : yuan(totalAmount)}</p>
        <p className="spend-range">
          <time dateTime={days[0].date}>{days[0].date}</time> 至{' '}
          <time dateTime={days.at(-1).date}>{days.at(-1).date}</time>
        </p>
      </div>
      <div className={`spend-frame${ticks.length === 1 ? ' spend-baseline' : ''}`}>
        <div className="spend-axis" aria-hidden="true">
          {ticks.map((value) => (
            <span key={value}>¥{value}</span>
          ))}
        </div>
        <div className="spend-plot">
          <div className="spend-gridlines" aria-hidden="true">
            {ticks.map((value) => (
              <span key={value} />
            ))}
          </div>
          <div className="spend-days" role="group" aria-label="每日折后费用，按模型堆叠">
            {days.map((day) => (
              <SpendDay key={day.date} day={day} limit={limit} />
            ))}
          </div>
        </div>
      </div>
      <div className="spend-footer">
        {models.length > 0 && (
          <ul className="spend-legend" aria-label="模型图例">
            {models.map((model) => (
              <li key={model}>
                <span aria-hidden="true" style={{ backgroundColor: modelColor(model) }} />
                {model}
              </li>
            ))}
          </ul>
        )}
        <p>{pendingCalls > 0 && `${formatNumber(pendingCalls)} 笔待计价 · `}选择日期查看调用记录</p>
      </div>
    </div>
  );
}
