const choose = document.querySelector('#files');
const declarations = document.querySelector('#symbols');
const query = document.querySelector('#find');
const esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
function list() {
  const term = query.value.toLowerCase();
  const selected = choose.value;
  const matches = files.filter(file => file.path.toLowerCase().includes(term) || file.symbols.some(symbol => symbol.name.toLowerCase().includes(term)));
  choose.innerHTML = matches.length ? matches.map(file => `<option value="${esc(file.path)}">${esc(file.path)}</option>`).join('') : '<option value="">没有匹配的源码</option>';
  choose.disabled = matches.length === 0;
  declarations.disabled = matches.length === 0;
  if (matches.some(file => file.path === selected)) choose.value = selected;
  return matches;
}
function view() {
  const params = new URLSearchParams(location.hash.slice(1));
  const [pathname, symbolName] = (params.get('symbol') || '').split('::');
  const file = files.find(file => file.path === (pathname || params.get('file'))) || files.find(file => file.path === 'src/app/bootstrap.py') || files[0];
  const symbol = file.symbols.find(symbol => symbol.name === symbolName);
  if (![...choose.options].some(option => option.value === file.path)) { query.value = ''; list(); }
  choose.value = file.path;
  declarations.innerHTML = '<option value="">完整文件</option>' + file.symbols.map(symbol => `<option value="${esc(symbol.name)}">${esc((symbol.kind === 'class' ? '类 ' : '函数 ') + symbol.name + ' L' + symbol.start)}</option>`).join('');
  declarations.value = symbol ? symbol.name : '';
  const lines = file.code.split('\n');
  const start = symbol ? symbol.start : 1, end = symbol ? symbol.end : lines.length;
  const url = `${repository}/blob/${revision}/${file.path}#L${start}-L${end}`;
  let calls = '';
  if (symbol && symbol.calls) {
    const project = [], dynamic = [];
    for (const call of symbol.calls) {
      if (!call.target) { dynamic.push(`<li><code>${esc(call.expression)}</code> · L${call.line}</li>`); continue; }
      const target = call.target;
      const purpose = explanations[target.file + '::' + target.name] || '';
      project.push(`<div class="symbol-call"><code>${esc(call.expression)}</code> · L${call.line}<p><a href="#symbol=${encodeURIComponent(target.file + '::' + target.name)}">${esc(target.name + ' · ' + target.file)}</a>${purpose ? '<br><span class="caption">' + esc(purpose) + '</span>' : ''}</p></div>`);
    }
    calls = '<h2>这段代码里能定位到的调用</h2><p class="caption">下面是从源码中找到的调用线索：条件分支可能不执行，返回的方法也可能稍后才调用，因此不能把列表当作先后顺序。传进来的对象究竟使用哪种实现，请回到组装章和各步导读核对。</p>' + (project.join('') || '<p>这里没有能从源码直接确定位置的项目内调用；仍可能通过传入的方法调用其他模块。</p>')
      + `<details><summary>查看 ${dynamic.length} 项暂时不能直接跳转的调用</summary><ul>${dynamic.join('')}</ul><p class="caption">不能直接跳转，不等于来自外部库。例如launcher、模型和工具可能由应用传入，需回到组装章节找实际对象。</p></details>`;
  }
  const purpose = symbol && (explanations[file.path + '::' + symbol.name] || symbol.comment);
  const overloadNotice = symbol?.overloads?.length ? '<p class="caption">此定义另有类型重载签名，完整文件中保留所有重载；当前显示真实实现。</p>' : '';
  const branchNotice = symbol?.ambiguous_branch ? '<p class="caption">条件分支内同名定义，按源行区分；静态索引不猜测运行时选择。</p>' : '';
  document.querySelector('#content').innerHTML = `<h1>${esc(symbol ? symbol.name : file.path)}</h1><p class="source-links"><a href="${esc(url)}">GitHub 固定版本 L${start}</a><a href="#file=${encodeURIComponent(file.path)}">完整文件</a></p>${purpose ? '<p class="notice">' + esc(purpose) + '</p>' : ''}${overloadNotice}${branchNotice}<pre><code>${lines.slice(start - 1, end).map((line, i) => `<span class="line" id="L${start + i}"><span class="line-number">${start + i}</span>${esc(line)}</span>`).join('')}</code></pre>${calls}`;
  document.title = (symbol ? symbol.name : file.path) + ' · pycode 源码';
}
choose.onchange = () => { if (choose.value) location.hash = 'file=' + encodeURIComponent(choose.value); };
declarations.onchange = () => { location.hash = declarations.value ? 'symbol=' + encodeURIComponent(choose.value + '::' + declarations.value) : 'file=' + encodeURIComponent(choose.value); };
query.oninput = () => {
  const previous = choose.value;
  const matches = list();
  if (!matches.length) {
    declarations.innerHTML = '<option value="">无匹配定义</option>';
    document.querySelector('#content').innerHTML = '<h1>没有匹配的源码</h1><p>请缩短路径或函数名，再选择源码。</p>';
    return;
  }
  if (!matches.some(file => file.path === previous)) {
    const next = '#file=' + encodeURIComponent(matches[0].path);
    if (location.hash === next) view();
    else location.hash = next;
  }
};
addEventListener('hashchange', view);
list(); view();
// Keep fragment navigation inside the offline viewer, including embedded browser hosts.
document.addEventListener('click', event => {
  const link = event.target.closest('a[href^="#symbol="], a[href^="#file="]');
  if (!link) return;
  event.preventDefault();
  const next = link.getAttribute('href');
  if (location.hash === next) view();
  else location.hash = next;
});
