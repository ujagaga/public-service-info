document.addEventListener('DOMContentLoaded', () => {
  // Text fields keep dd.mm.yyyy regardless of browser locale; the hidden native input only supplies the calendar.
  document.querySelectorAll('.date-field').forEach(field => {
    const text = field.querySelector('input[type=text]');
    const picker = field.querySelector('.date-picker');
    field.querySelector('.date-picker-button').addEventListener('click', () => {
      const [day, month, year] = text.value.split('.');
      picker.value = year && month && day ? `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')}` : '';
      picker.showPicker();
    });
    picker.addEventListener('change', () => {
      if (!picker.value) return;
      const [year, month, day] = picker.value.split('-');
      text.value = `${day}.${month}.${year}`;
    });
  });
});
