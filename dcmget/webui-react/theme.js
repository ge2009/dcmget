(function () {
  try {
    var saved = localStorage.getItem('dcmget-theme');
    var dark = saved === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  } catch (_) {
    document.documentElement.dataset.theme = 'light';
  }
})();
