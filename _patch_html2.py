import re
import pathlib

p = pathlib.Path('app/templates/admin_dailycroc.html')
content = p.read_text(encoding='utf-8')

# Add nonces to style and script
content = content.replace('<style>', '<style nonce="{{ g.csp_nonce }}">')
content = content.replace('<script>', '<script nonce="{{ g.csp_nonce }}">')

# Fix refresh button
content = content.replace(
    '<button class="btn" style="width:auto;padding:6px 12px;" onclick="loadPuzzles()">🔄</button>',
    '<button class="btn btn-refresh" data-action="loadPuzzles">🔄</button>'
)

# Add CSS class for btn-refresh, edit-hidden, save-btn, mt-8
css_add = """
.btn-refresh { width: auto; padding: 6px 12px; }
.edit-hidden { display: none; }
.btn-save { background: var(--link); color: #fff; }
.mt-8 { margin-top: 8px; }
"""
content = content.replace('</style>', css_add + '</style>')

# Fix renderPuzzles string
content = content.replace(
    '''<button class="btn-text" onclick="editPrompt('${p.date}', '${p.difficulty}')">✏️ Edit</button>''',
    '''<button class="btn-text" data-action="editPrompt" data-date="${p.date}" data-diff="${p.difficulty}">✏️ Edit</button>'''
)

content = content.replace(
    '''<div class="prompt-edit" id="prompt-edit-${p.date}-${p.difficulty}" style="display:none;">''',
    '''<div class="prompt-edit edit-hidden" id="prompt-edit-${p.date}-${p.difficulty}">'''
)

content = content.replace(
    '''<button class="btn" style="background:var(--link);color:#fff" onclick="savePrompt('${p.date}', '${p.difficulty}')">Save</button>''',
    '''<button class="btn btn-save" data-action="savePrompt" data-date="${p.date}" data-diff="${p.difficulty}">Save</button>'''
)

content = content.replace(
    '''<button class="btn" onclick="cancelEditPrompt('${p.date}', '${p.difficulty}')">Cancel</button>''',
    '''<button class="btn" data-action="cancelPrompt" data-date="${p.date}" data-diff="${p.difficulty}">Cancel</button>'''
)

content = content.replace(
    '''<div class="prompt-section" style="margin-top:8px">''',
    '''<div class="prompt-section mt-8">'''
)

content = content.replace(
    '''onchange="saveModel('${p.date}', '${p.difficulty}', this.value)"''',
    '''data-action="saveModel" data-date="${p.date}" data-diff="${p.difficulty}"'''
)

content = content.replace(
    '''onclick="regenerate('${p.date}', '${p.difficulty}')"''',
    '''data-action="regen" data-date="${p.date}" data-diff="${p.difficulty}"'''
)

content = content.replace(
    '''onclick="resetWord('${p.date}', '${p.difficulty}')"''',
    '''data-action="reset" data-date="${p.date}" data-diff="${p.difficulty}"'''
)

# Replace inline style.display assignments in editPrompt and cancelEditPrompt
content = content.replace(
    "document.getElementById(`prompt-text-${date}-${diff}`).style.display = 'none';",
    "document.getElementById(`prompt-text-${date}-${diff}`).classList.add('edit-hidden');"
)
content = content.replace(
    "document.getElementById(`prompt-edit-${date}-${diff}`).style.display = 'block';",
    "document.getElementById(`prompt-edit-${date}-${diff}`).classList.remove('edit-hidden');"
)
content = content.replace(
    "document.getElementById(`prompt-text-${date}-${diff}`).style.display = 'block';",
    "document.getElementById(`prompt-text-${date}-${diff}`).classList.remove('edit-hidden');"
)
content = content.replace(
    "document.getElementById(`prompt-edit-${date}-${diff}`).style.display = 'none';",
    "document.getElementById(`prompt-edit-${date}-${diff}`).classList.add('edit-hidden');"
)


# Add global event listeners at the end of the script
js_add = """
document.addEventListener('click', (e) => {
  const target = e.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;
  const date = target.dataset.date;
  const diff = target.dataset.diff;

  if (action === 'regen') regenerate(date, diff);
  else if (action === 'reset') resetWord(date, diff);
  else if (action === 'editPrompt') editPrompt(date, diff);
  else if (action === 'savePrompt') savePrompt(date, diff);
  else if (action === 'cancelPrompt') cancelEditPrompt(date, diff);
  else if (action === 'loadPuzzles') loadPuzzles();
});

document.addEventListener('change', (e) => {
  if (e.target.matches('.model-select')) {
    saveModel(e.target.dataset.date, e.target.dataset.diff, e.target.value);
  }
});
"""
content = content.replace('loadPuzzles();\n</script>', 'loadPuzzles();\n' + js_add + '\n</script>')

p.write_text(content, encoding='utf-8')
print("Done")
