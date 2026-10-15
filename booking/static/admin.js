import {api, money, dateTime, node, notice} from './common.js';
const $ = selector => document.querySelector(selector);
function view(authorized) {
  $('#login-panel').hidden = authorized; $('#admin-panel').hidden = !authorized;
}
async function refresh(silent = false) {
  try {
    const date = $('#filter-date').value;
    const bookings = await api('/api/admin/bookings' + (date ? '?day=' + encodeURIComponent(date) : ''));
    view(true); $('#admin-rows').replaceChildren();
    for (const booking of bookings) {
      const row = node('tr');
      row.append(node('td', dateTime(booking.starts_at)), node('td', booking.service_title),
        node('td', money(booking.price_rub)));
      const status = node('td'); status.append(node('span', booking.status === 'confirmed' ? 'Подтверждена' : 'Отменена',
        'status' + (booking.status === 'cancelled' ? ' cancelled' : '')));
      row.append(status, node('td', booking.id)); $('#admin-rows').append(row);
    }
    $('#empty-admin').hidden = bookings.length > 0;
  } catch (error) {
    if ([401, 403].includes(error.status)) view(false);
    if (!silent) notice(error.message, true);
  }
}
$('#login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('#login-button').disabled = true;
  try {
    await api('/api/admin/login', {method: 'POST', body: {access_key: $('#access-key').value}});
    $('#access-key').value = ''; $('#notice').hidden = true; await refresh();
  } catch (error) {notice(error.message, true);}
  finally {$('#login-button').disabled = false;}
});
$('#logout').addEventListener('click', async () => {
  try {await api('/api/admin/logout', {method: 'POST'}); view(false); $('#admin-rows').replaceChildren(); notice('Вы вышли из административной части.');}
  catch (error) {notice(error.message, true);}
});
$('#filter-date').addEventListener('change', () => refresh());
$('#clear-filter').addEventListener('click', () => {$('#filter-date').value = ''; refresh();});
$('#refresh').addEventListener('click', () => refresh());
refresh(true);

