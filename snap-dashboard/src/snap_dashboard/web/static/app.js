/**
 * snap-dashboard — minimal vanilla JS
 */

(function () {
  'use strict';

  // ---- Header user menu (avatar/name -> logout popover) -----
  // Small adaptation of Vanilla Framework's contextual-menu pattern,
  // scoped to the nav user menu toggle.
  (function () {
    var toggle = document.querySelector('.nav-user-toggle');
    if (!toggle) return;
    var dropdown = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!dropdown) return;

    function setOpen(open) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      dropdown.setAttribute('aria-hidden', open ? 'false' : 'true');
    }

    toggle.addEventListener('click', function (event) {
      event.preventDefault();
      setOpen(toggle.getAttribute('aria-expanded') !== 'true');
    });

    document.addEventListener('click', function (event) {
      if (!toggle.contains(event.target) && !dropdown.contains(event.target)) {
        setOpen(false);
      }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        setOpen(false);
      }
    });
  })();

  // ---- Intersection observer fade-in on .card elements -----
  if ('IntersectionObserver' in window) {
    // Mark cards for animation
    document.querySelectorAll('.card').forEach(function (card) {
      card.classList.add('fade-in');
    });

    const observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add('visible');
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.1 }
    );

    document.querySelectorAll('.card.fade-in').forEach(function (card) {
      observer.observe(card);
    });
  }

})();
