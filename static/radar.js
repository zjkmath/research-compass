/* Optional enhancement only; core navigation/forms use Django server responses. */
document.querySelectorAll('[data-compare-search]').forEach((input) => {
  const box = document.getElementById('id_choices');
  if (!box) return;
  input.parentElement.hidden = false;
  input.addEventListener('input', () => {
    const words = input.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    box.querySelectorAll('label').forEach((label) => {
      const control = label.querySelector('input');
      label.parentElement.hidden = !control.checked && !words.every((w) => label.textContent.toLocaleLowerCase().includes(w));
    });
  });
});
(() => {
  const toggles = document.querySelectorAll('[data-effects-toggle]');
  toggles.forEach((toggle) => {
    toggle.hidden = false;
    toggle.addEventListener('click', () => {
      const off = document.body.classList.toggle('effects-off');
      toggles.forEach((item) => {
        item.setAttribute('aria-pressed', String(off));
        item.textContent = off ? '开启增强效果' : '关闭增强效果';
      });
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const open = event.target.closest('details[open]');
    if (open) {
      open.open = false;
      open.querySelector('summary')?.focus();
    }
  });
})();

// A focused wide table has a predictable keyboard scroll step. Links inside
// retain their usual keys; the native scrollbar still works without scripts.
document.querySelectorAll('.table-scroll').forEach((table) => {
  table.addEventListener('keydown', (event) => {
    if (event.target !== table || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    table.scrollBy({left: event.key === 'ArrowRight' ? 160 : -160, behavior: 'instant'});
  });
});

// Server forms remain usable without enhancement. Only unsaved edits prompt locally.
(() => { let dirty=false; document.querySelectorAll('form[data-dirty-warning]').forEach(form=>{ form.addEventListener('change',()=>{dirty=true}); form.addEventListener('input',()=>{dirty=true}); form.addEventListener('submit',()=>{dirty=false}); }); window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}}); })();
