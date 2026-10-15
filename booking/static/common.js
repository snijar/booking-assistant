export async function api(path, {method = 'GET', body, key} = {}) {
  const headers = {'X-Requested-With': 'booking-assistant'};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (key) headers['Idempotency-Key'] = key;
  let response;
  try {
    response = await fetch(path, {method, headers, credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body)});
  } catch {
    throw new Error('Нет связи с сервером. Проверьте подключение и повторите действие.');
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || 'Не удалось выполнить запрос.');
    error.status = response.status;
    throw error;
  }
  return data;
}

export const money = value => new Intl.NumberFormat('ru-RU').format(value) + ' ₽';
export const dateTime = value => new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit',
}).format(new Date(value));
export const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
export function notice(message, error = false) {
  const area = document.querySelector('#notice');
  area.textContent = message;
  area.className = error ? 'notice error' : 'notice';
  area.setAttribute('role', error ? 'alert' : 'status');
  area.hidden = false;
}
export function tomorrow(day) {
  const date = new Date(day + 'T12:00:00Z');
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

