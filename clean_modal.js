document.querySelectorAll('.modal, .modal-backdrop, .fade.show, [class*="modal"], [class*="backdrop"], [class*="popup"], [class*="overlay"]').forEach(e => e.remove());
document.body.style.overflow = 'auto';
document.body.style.position = 'static';
document.documentElement.style.overflow = 'auto';
