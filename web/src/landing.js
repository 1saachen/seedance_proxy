const terminal = document.querySelector('.landing-terminal');
const tabList = terminal.querySelector('.terminal-tabs');
const tabs = [...tabList.querySelectorAll('button')];
const panels = tabs.map((tab) => document.getElementById(tab.getAttribute('aria-controls')));
const copyButton = terminal.querySelector('.terminal-copy');
const feedback = terminal.querySelector('.terminal-feedback');
let active = 0;

function showExample(index) {
  active = index;
  tabs.forEach((tab, position) => {
    const selected = position === active;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    panels[position].hidden = !selected;
  });
  feedback.textContent = '';
}

tabs.forEach((tab, index) => {
  panels[index].setAttribute('role', 'tabpanel');
  panels[index].setAttribute('aria-labelledby', tab.id);
  tab.addEventListener('click', () => showExample(index));
});

tabList.addEventListener('keydown', (event) => {
  const index = tabs.indexOf(event.target);
  const next = {
    ArrowLeft: (index - 1 + tabs.length) % tabs.length,
    ArrowRight: (index + 1) % tabs.length,
    Home: 0,
    End: tabs.length - 1,
  }[event.key];
  if (index < 0 || next === undefined) return;
  event.preventDefault();
  showExample(next);
  tabs[next].focus();
});

if (navigator.clipboard?.writeText) {
  copyButton.hidden = false;
  copyButton.addEventListener('click', async () => {
    const index = active;
    const command = panels[index].querySelector('code').textContent;
    try {
      await navigator.clipboard.writeText(command);
      if (index === active) feedback.textContent = '已复制';
    } catch {
      if (index === active) feedback.textContent = '请选中命令后复制';
    }
  });
}

terminal.querySelector('[data-api-origin]').textContent = window.location.origin;
terminal.classList.add('has-tabs');
tabList.hidden = false;
showExample(0);
