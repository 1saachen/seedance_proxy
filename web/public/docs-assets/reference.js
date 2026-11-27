if (navigator.clipboard?.writeText) {
  for (const example of document.querySelectorAll('.docs-example')) {
    const caption = example.querySelector('figcaption');
    const code = example.querySelector('pre code');
    const button = document.createElement('button');
    const announcement = document.createElement('span');
    button.type = 'button';
    button.className = 'docs-copy';
    button.textContent = '复制';
    button.setAttribute('aria-label', `复制${caption.textContent.trim()}`);
    announcement.className = 'docs-copy';
    announcement.setAttribute('aria-live', 'polite');
    button.addEventListener('click', async () => {
      announcement.textContent = '';
      try {
        await navigator.clipboard.writeText(code.textContent);
        announcement.textContent = '已复制';
      } catch {
        announcement.textContent = '请选中代码后复制';
      }
    });
    const actions = document.createElement('span');
    actions.append(announcement, button);
    caption.append(actions);
  }
}
