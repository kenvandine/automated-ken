/**
 * snap-dashboard — minimal vanilla JS
 */

(function () {
  'use strict';

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
