import {api, money, dateTime, node, notice, tomorrow} from './common.js';

let services = [], selectedService = null, selectedSlot = null, config;
let moveId = null, moveSlot = null;
const $ = selector => document.querySelector(selector);
const requestKeys = new Map();
const generations = new WeakMap();
async function mutation(path, body) {
  const signature = JSON.stringify([path, body]);
  if (!requestKeys.has(signature)) requestKeys.set(signature, crypto.randomUUID());
  const result = await api(path, {method: 'POST', body, key: requestKeys.get(signature)});
  requestKeys.delete(signature);
  return result;
}
function updateSummary() {
  const service = services.find(s => s.id === selectedService);
  $('#selection').textContent = service && selectedSlot
    ? `${service.title} · ${dateTime(selectedSlot)} · ${money(service.price_rub)}`
    : 'Выберите услугу и время.';
  $('#book-button').disabled = !(service && selectedSlot);
}
async function loadSlots(container, day, select, selected = null) {
  const generation = (generations.get(container) || 0) + 1;
  generations.set(container, generation);
  container.setAttribute('aria-busy', 'true');
  container.replaceChildren(node('p', 'Загружаем время…', 'muted'));
  try {
    const slots = await api('/api/slots?day=' + encodeURIComponent(day));
    if (generations.get(container) !== generation) return;
    container.replaceChildren();
    if (!slots.length) container.append(node('p', 'На сегодня свободного времени больше нет. Выберите другую дату.', 'empty'));
    for (const slot of slots) {
      const button = node('button', slot.local_time, 'slot');
      button.type = 'button'; button.disabled = !slot.available;
      button.setAttribute('aria-label', slot.local_time + (slot.available ? ' МСК' : ' МСК — занято'));
      button.setAttribute('aria-pressed', String(slot.starts_at === selected));
      button.addEventListener('click', () => {
        container.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', 'false'));
        button.setAttribute('aria-pressed', 'true'); select(slot.starts_at);
      });
      container.append(button);
    }
  } catch (error) {
    if (generations.get(container) === generation) {
      container.replaceChildren(node('p', error.message, 'inline-error'));
    }
  } finally {
    if (generations.get(container) === generation) container.setAttribute('aria-busy', 'false');
  }
}
async function refreshMainSlots() {
  selectedSlot = null; updateSummary();
  await loadSlots($('#slots'), $('#booking-date').value, slot => {selectedSlot = slot; updateSummary();});
}
async function refreshBookings() {
  const container = $('#bookings'); container.setAttribute('aria-busy', 'true');
  try {
    const bookings = await api('/api/bookings');
    container.replaceChildren();
    if (!bookings.length) container.append(node('p', 'Пока нет записей. Выберите время — подтверждение появится здесь.', 'empty'));
    for (const booking of bookings) {
      const row = node('article', undefined, 'booking'); row.dataset.id = booking.id;
      row.append(node('span', booking.status === 'confirmed' ? 'Подтверждена' : 'Отменена',
        'status' + (booking.status === 'cancelled' ? ' cancelled' : '')),
      node('h3', booking.service_title), node('p', dateTime(booking.starts_at) + ' МСК'),
      node('p', '60 минут · ' + money(booking.price_rub), 'muted'),
      node('p', booking.id, 'booking-id'));
      if (booking.status === 'confirmed' && new Date(booking.starts_at) > new Date()) {
        const actions = node('div', undefined, 'booking-actions');
        const move = node('button', 'Перенести', 'text-button'); move.type = 'button';
        move.addEventListener('click', () => openMove(booking));
        const cancel = node('button', 'Отменить', 'text-button danger'); cancel.type = 'button';
        cancel.addEventListener('click', async () => {
          if (cancel.dataset.confirm !== 'yes') {
            cancel.dataset.confirm = 'yes'; cancel.textContent = 'Да, отменить'; return;
          }
          cancel.disabled = true;
          try {
            await mutation(`/api/bookings/${booking.id}/cancel`);
            notice('Запись отменена. Время снова доступно для записи.');
            await Promise.all([refreshBookings(), refreshMainSlots()]);
          } catch (error) { notice(error.message, true); cancel.disabled = false; }
        });
        actions.append(move, cancel); row.append(actions);
      }
      container.append(row);
    }
  } catch (error) {container.replaceChildren(node('p', error.message, 'inline-error'));}
  finally {container.setAttribute('aria-busy', 'false');}
}
function refreshMoveSlots() {
  moveSlot = null; $('#move-button').disabled = true;
  return loadSlots($('#move-slots'), $('#move-date').value, slot => {
    moveSlot = slot; $('#move-button').disabled = false;
  });
}
function openMove(booking) {
  moveId = booking.id; $('#move-date').value = $('#booking-date').value;
  $('#move-error').hidden = true; $('#move-dialog').showModal(); refreshMoveSlots();
}
$('#booking-form').addEventListener('submit', async event => {
  event.preventDefault(); if (!selectedService || !selectedSlot) return;
  $('#book-button').disabled = true; $('#book-button').textContent = 'Сохраняем…';
  try {
    const booking = await mutation('/api/bookings', {service_id: selectedService, starts_at: selectedSlot});
    notice('Запись подтверждена: ' + dateTime(booking.starts_at) + ' МСК.');
    await Promise.all([refreshBookings(), refreshMainSlots()]);
  } catch (error) {notice(error.message, true); await refreshMainSlots();}
  finally {$('#book-button').textContent = 'Подтвердить запись'; updateSummary();}
});
$('#move-form').addEventListener('submit', async event => {
  event.preventDefault(); if (!moveSlot || !moveId) return;
  $('#move-button').disabled = true;
  try {
    const booking = await mutation(`/api/bookings/${moveId}/reschedule`, {starts_at: moveSlot});
    $('#move-dialog').close(); notice('Запись перенесена: ' + dateTime(booking.starts_at) + ' МСК.');
    await Promise.all([refreshBookings(), refreshMainSlots()]);
  } catch (error) {
    $('#move-error').textContent = error.message; $('#move-error').hidden = false;
    await refreshMoveSlots();
  }
});
$('#close-move').addEventListener('click', () => $('#move-dialog').close());
$('#move-date').addEventListener('change', refreshMoveSlots);
$('#booking-date').addEventListener('change', refreshMainSlots);
$('#tomorrow').addEventListener('click', () => {$('#booking-date').value = tomorrow(config.today); refreshMainSlots();});
$('#refresh').addEventListener('click', async () => {await Promise.all([refreshBookings(), refreshMainSlots()]);});

async function initialize() {
  try {
    await api('/api/session', {method: 'POST'});
    [config, services] = await Promise.all([api('/api/config'), api('/api/services')]);
    for (const input of [$('#booking-date'), $('#move-date')]) {
      input.min = config.today; input.max = config.last_day; input.value = tomorrow(config.today);
    }
    const serviceContainer = $('#services'); serviceContainer.replaceChildren();
    services.forEach((service, index) => {
      const label = node('label', undefined, 'service');
      const radio = node('input'); radio.type = 'radio'; radio.name = 'service'; radio.value = service.id;
      radio.checked = index === 0; if (radio.checked) selectedService = service.id;
      radio.addEventListener('change', () => {selectedService = service.id; updateSummary();});
      const text = node('span'); text.append(node('strong', service.title), node('small', service.description));
      label.append(radio, text, node('span', money(service.price_rub), 'price')); serviceContainer.append(label);
    });
    serviceContainer.setAttribute('aria-busy', 'false');
    await Promise.all([refreshMainSlots(), refreshBookings()]);
  } catch (error) {
    notice(error.message, true); $('#services').setAttribute('aria-busy', 'false');
    $('#services').replaceChildren(node('p', 'Не удалось загрузить приложение. Обновите страницу.', 'inline-error'));
    $('#slots').replaceChildren(); $('#slots').setAttribute('aria-busy', 'false');
    $('#bookings').replaceChildren(); $('#bookings').setAttribute('aria-busy', 'false');
  }
}
initialize();

