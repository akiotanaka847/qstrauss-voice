function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  event.target.classList.add('active');
}

// Auto-detect platform and highlight the right button
(function() {
  var isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
  document.getElementById(isMac ? 'btn-mac' : 'btn-win').classList.add('recommended');
})();
