/* Shared interaction behaviour, loaded on every page via templates/_ui_foot.html.
   Plain JavaScript, no libraries. Everything here is an enhancement: with
   JavaScript off, every form still works as an ordinary post + redirect. */
(function () {
  'use strict';

  /* ---------- Toasts ---------- */
  var toastBox = document.getElementById('ggToasts');

  function showToast(message, type) {
    if (!toastBox) return;
    var toast = document.createElement('div');
    toast.className = 'gg-toast' + (type === 'error' ? ' is-error' : '');
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    toastBox.appendChild(toast);
    requestAnimationFrame(function () { toast.classList.add('is-visible'); });
    setTimeout(function () {
      toast.classList.remove('is-visible');
      setTimeout(function () { toast.remove(); }, 250);
    }, 2600);
  }
  window.ggToast = showToast;

  /* ---------- Save to wishlist without a page reload ---------- */
  function isWishlistForm(form) {
    return form.tagName === 'FORM' && /\/wishlist\/.+\/toggle/.test(form.getAttribute('action') || '');
  }

  function updateWishlistBadge(count) {
    document.querySelectorAll('[data-wishlist-count]').forEach(function (badge) {
      badge.textContent = count;
      badge.hidden = !count;
    });
  }

  function paintWishButton(button, saved) {
    button.classList.toggle('saved', saved);
    var label = saved ? button.getAttribute('data-saved-label') : button.getAttribute('data-unsaved-label');
    if (label !== null) {
      button.textContent = label;              // detail pages: "♥ Saved to wishlist"
    } else {
      button.textContent = saved ? '♥' : '♡';  // cards: heart only
    }
    button.title = saved ? 'Remove from wishlist' : 'Save to wishlist';
  }

  function submitWishlist(form) {
    var button = form.querySelector('button');
    if (button) button.classList.add('is-busy');

    fetch(form.action, {
      method: 'POST',
      body: new FormData(form),
      headers: { 'X-Requested-With': 'fetch' },
      credentials: 'same-origin'
    })
      .then(function (response) {
        if (!response.ok) throw new Error('bad response');
        return response.json();
      })
      .then(function (data) {
        showToast(data.message);
        updateWishlistBadge(data.count);
        var card = form.closest('[data-wish-card]');
        if (form.hasAttribute('data-remove-card') && !data.saved && card) {
          // On the Wishlist page itself, un-saving removes the card.
          var grid = card.parentElement;
          card.remove();
          if (grid && !grid.querySelector('[data-wish-card]')) window.location.reload();
        } else if (button) {
          paintWishButton(button, data.saved);
        }
      })
      .catch(function () {
        // Anything unexpected (session expired, network blip): fall back to
        // the normal full-page post so the action still goes through.
        form.submit();
      })
      .then(function () {
        if (button) button.classList.remove('is-busy');
      });
  }

  /* ---------- One submit handler for the whole site ---------- */
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented) return;  // a page's own handler (e.g. confirm()) already stopped it

    if (isWishlistForm(form)) {
      event.preventDefault();
      submitWishlist(form);
      return;
    }

    // Lock the submit button so a double-click can't create two bookings,
    // two payments or two support tickets. Deferred by one tick so the
    // button's own name/value is still sent with the form.
    if ((form.method || '').toLowerCase() !== 'post' || form.hasAttribute('data-no-lock')) return;
    var buttons = form.querySelectorAll('button[type=submit], button:not([type]), input[type=submit]');
    setTimeout(function () {
      buttons.forEach(function (button) {
        button.classList.add('is-loading');
        button.setAttribute('aria-disabled', 'true');
        if (button.tagName === 'BUTTON' && !button.hasAttribute('data-keep-label') && button.textContent.trim().length > 2) {
          button.setAttribute('data-original-label', button.textContent);
          button.textContent = 'Please wait…';
        }
      });
    }, 0);
  });

  // Coming back with the browser's Back button restores the page from cache
  // with the buttons still locked -- unlock them.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('.is-loading').forEach(function (button) {
      button.classList.remove('is-loading');
      button.removeAttribute('aria-disabled');
      var original = button.getAttribute('data-original-label');
      if (original !== null) button.textContent = original;
    });
  });

  /* ---------- Back to top ---------- */
  var top = document.createElement('button');
  top.type = 'button';
  top.className = 'gg-top';
  top.setAttribute('aria-label', 'Back to top');
  top.textContent = '↑';
  top.addEventListener('click', function () { window.scrollTo({ top: 0, behavior: 'smooth' }); });
  document.body.appendChild(top);
  window.addEventListener('scroll', function () {
    top.classList.toggle('is-visible', window.scrollY > 600);
  }, { passive: true });
})();
