(function () {
  var toggle = document.getElementById('sidebarToggle');
  var backdrop = document.getElementById('sidebarBackdrop');
  if (!toggle) return;

  toggle.addEventListener('click', function () {
    document.body.classList.toggle('sidebar-collapsed');
    document.body.classList.toggle('sidebar-mobile-open');
  });

  if (backdrop) {
    backdrop.addEventListener('click', function () {
      document.body.classList.remove('sidebar-collapsed', 'sidebar-mobile-open');
    });
  }
})();
