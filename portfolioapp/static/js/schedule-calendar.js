/*
 * Shared "pick a date & time" calendar widget.
 *
 * Used by both schedule.html and schedule_manage.html.
 * The two pages differ only in the
 * data they inject via window.SCHEDULE_CONFIG:
 *
 *   initialDate           (required) "YYYY-MM-DD" - month the calendar opens on
 *   availabilityUrl       (required) URL of the availability JSON endpoint
 *   timeSlots             (required) array of time-slot labels, e.g. ["9:00 AM", ...]
 *   slotAvailableLabel    (optional) text appended to an open slot's button,
 *                          e.g. "Click to Schedule" vs "Click to Select"
 *                          (defaults to "Click to Schedule")
 *   initialSelectedDate   (optional) "YYYY-MM-DD" - pre-select a date (reschedule)
 *   initialSelectedTime   (optional) pre-select a time (reschedule)
 *   excludeBookingNumber  (optional) passed through to the availability endpoint
 *                          so an appointment's own current slot doesn't look
 *                          "taken by itself" (reschedule)
 *
 * Requires these elements to exist in the page:
 *   #calendar, #calendar-month-label, #previous-month, #next-month,
 *   #date-select, #time-select, #schedule-submit, #selection-message,
 *   #calendar-toggle, #calendar-wrapper, and one or more
 *   .calendar-view-button[data-view] buttons.
 */
document.addEventListener("DOMContentLoaded", () => {

  const config = window.SCHEDULE_CONFIG;
  if (!config) {
    return; // nothing to do on pages that don't set up a calendar
  }

  const calendar = document.getElementById("calendar");
  const monthLabel = document.getElementById("calendar-month-label");

  const previousButton = document.getElementById("previous-month");
  const nextButton = document.getElementById("next-month");

  const dateSelect = document.getElementById("date-select");
  const timeSelect = document.getElementById("time-select");

  const submitButton = document.getElementById("schedule-submit");
  const selectionMessage = document.getElementById("selection-message");

  const calendarToggle = document.getElementById("calendar-toggle");
  const calendarWrapper = document.getElementById("calendar-wrapper");

  const slotAvailableLabel = config.slotAvailableLabel || "Click to Schedule";

  let currentDate = new Date(config.initialDate + "T12:00:00");

  // Pre-fill with an existing selection (reschedule flow) so a homeowner
  // who just wants to double check things sees their existing slot
  // already selected, rather than a blank picker.
  let selectedDate = config.initialSelectedDate
    ? new Date(config.initialSelectedDate + "T12:00:00")
    : null;
  let selectedTime = config.initialSelectedTime || null;
  let currentView = "month";

  const timeSlots = config.timeSlots;

  /*
   * AVAILABILITY
   *
   * Real availability lives server-side (see availability.py + the
   * /schedule/availability/ endpoint), so this is the single source of
   * truth the calendar and the final booking submission both agree on.
   * We cache per-month responses to avoid refetching the same month
   * repeatedly while paging around.
   */
  const availabilityCache = {};

  function monthKey(year, monthIndexZeroBased) {
    return `${year}-${String(monthIndexZeroBased + 1).padStart(2, "0")}`;
  }

  async function fetchMonthAvailability(year, monthIndexZeroBased) {
    const key = monthKey(year, monthIndexZeroBased);

    if (availabilityCache[key]) {
      return availabilityCache[key];
    }

    // The exclude param tells the server to treat this appointment's own
    // current slot as available (not "taken by itself"), so it shows up
    // pickable/keepable instead of looking unavailable on the calendar.
    const excludeParam = config.excludeBookingNumber
      ? `&exclude=${encodeURIComponent(config.excludeBookingNumber)}`
      : "";

    const response = await fetch(
      `${config.availabilityUrl}?month=${key}${excludeParam}`
    );

    const data = await response.json();

    availabilityCache[key] = data.available || {};

    return availabilityCache[key];
  }

  // Merge the availability of every month touched by `dates` into one
  // { "YYYY-MM-DD": ["10:00 AM", ...] } lookup.
  async function collectAvailability(dates) {

    const months = new Set(
      dates.map(d => monthKey(d.getFullYear(), d.getMonth()))
    );

    const entries = await Promise.all(
      [...months].map(async key => {
        const [year, month] = key.split("-").map(Number);
        return fetchMonthAvailability(year, month - 1);
      })
    );

    return Object.assign({}, ...entries);
  }

  function formatDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function displayDate(date) {
    return date.toLocaleDateString("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric"
    });
  }

  function isAvailable(date, time, availabilityMap) {
    const times = availabilityMap[formatDate(date)] || [];
    return times.includes(time);
  }

  async function renderCalendar() {

    calendar.setAttribute("aria-busy", "true");

    const days = currentView === "week"
      ? weekDays(currentDate)
      : monthDays(currentDate);

    const availabilityMap = await collectAvailability(days);

    calendar.innerHTML = "";

    if (currentView === "week") {
      renderWeekView(days, availabilityMap);
    } else {
      renderMonthView(days, availabilityMap);
    }

    monthLabel.textContent = currentDate.toLocaleDateString("en-US", {
      month: "long",
      year: "numeric"
    });

    calendar.setAttribute("aria-busy", "false");
  }

  function monthDays(reference) {
    const year = reference.getFullYear();
    const month = reference.getMonth();
    const lastDay = new Date(year, month + 1, 0);

    const days = [];
    for (let dayNumber = 1; dayNumber <= lastDay.getDate(); dayNumber++) {
      days.push(new Date(year, month, dayNumber));
    }
    return days;
  }

  function weekDays(reference) {
    const weekStart = new Date(reference);
    weekStart.setDate(reference.getDate() - reference.getDay());

    const days = [];
    for (let i = 0; i < 7; i++) {
      const day = new Date(weekStart);
      day.setDate(weekStart.getDate() + i);
      days.push(day);
    }
    return days;
  }

  function createDay(day, availabilityMap) {

    const dayElement = document.createElement("div");
    dayElement.className = "calendar-day";

    const dayHeader = document.createElement("div");
    dayHeader.className = "calendar-day-header";

    dayHeader.textContent = day.toLocaleDateString("en-US", {
      weekday: "short"
    });

    const dateNumber = document.createElement("strong");
    dateNumber.textContent = day.getDate();

    dayHeader.appendChild(dateNumber);
    dayElement.appendChild(dayHeader);

    timeSlots.forEach(time => {

      const slot = document.createElement("button");
      slot.type = "button";
      slot.className = "calendar-slot";

      const available = isAvailable(day, time, availabilityMap);

      slot.textContent = available
        ? `${time}  •  ${slotAvailableLabel}`
        : `${time}  •  Unavailable`;

      if (!available) {
        slot.classList.add("unavailable");
        slot.disabled = true;
        dayElement.appendChild(slot);
        return;
      }

      slot.classList.add("available");

      if (
        selectedDate &&
        formatDate(selectedDate) === formatDate(day) &&
        selectedTime === time
      ) {
        slot.classList.add("selected");
      }

      slot.addEventListener("click", () => {
        selectAppointment(day, time);
      });

      dayElement.appendChild(slot);
    });

    return dayElement;
  }

  function renderMonthView(days, availabilityMap) {

    calendar.className = "calendar calendar-month-view";

    const grid = document.createElement("div");
    grid.className = "calendar-grid";

    [
      "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"
    ].forEach(day => {
      const heading = document.createElement("div");
      heading.className = "calendar-weekday";
      heading.textContent = day;
      grid.appendChild(heading);
    });

    const startingDay = days[0].getDay();

    for (let i = 0; i < startingDay; i++) {
      const filler = document.createElement("div");
      filler.className = "calendar-day calendar-day--filler";
      grid.appendChild(filler);
    }

    days.forEach(day => {
      grid.appendChild(createDay(day, availabilityMap));
    });

    calendar.appendChild(grid);
  }

  function renderWeekView(days, availabilityMap) {

    calendar.className = "calendar calendar-week-view";

    const grid = document.createElement("div");
    grid.className = "calendar-week-grid";

    days.forEach(day => {
      grid.appendChild(createDay(day, availabilityMap));
    });

    calendar.appendChild(grid);
  }

  async function selectAppointment(date, time) {

    selectedDate = new Date(date);
    selectedTime = time;

    await updateDateDropdown();
    await updateTimeDropdown();

    selectionMessage.textContent =
      `${displayDate(date)} at ${time}`;

    selectionMessage.classList.add("has-selection");

    submitButton.disabled = false;

    await renderCalendar();
  }

  async function updateDateDropdown() {
    const days = monthDays(currentDate);
    const availabilityMap = await collectAvailability(days);

    dateSelect.innerHTML = "";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "SELECT DATE";
    placeholder.disabled = true;

    if (!selectedDate) {
      placeholder.selected = true;
    }

    dateSelect.appendChild(placeholder);

    days.forEach(day => {
      const times = availabilityMap[formatDate(day)] || [];

      if (times.length === 0) {
        return;
      }

      const option = document.createElement("option");

      option.value = formatDate(day);
      option.textContent = displayDate(day);

      if (selectedDate && formatDate(selectedDate) === formatDate(day)) {
        option.selected = true;
      }

      dateSelect.appendChild(option);

    });

    if (dateSelect.options.length === 1) {

      const option = document.createElement("option");

      option.value = "";
      option.textContent = "NO AVAILABLE DATES";
      dateSelect.appendChild(option);
    }
  }

  async function updateTimeDropdown() {

    const availabilityMap = await collectAvailability([selectedDate]);

    timeSelect.disabled = false;
    timeSelect.innerHTML = "";

    timeSlots.forEach(time => {

      if (!isAvailable(selectedDate, time, availabilityMap)) {
        return;
      }

      const option = document.createElement("option");

      option.value = time;
      option.textContent = time;

      if (time === selectedTime) {
        option.selected = true;
      }

      timeSelect.appendChild(option);
    });

    if (![...timeSelect.options].some(opt => opt.value === selectedTime)) {
      selectedTime = timeSelect.options[0] ? timeSelect.options[0].value : null;
      if (selectedTime) {
        timeSelect.value = selectedTime;
      }
    }
  }

  function updateCalendarToggle() {
    const isHidden = calendarWrapper.classList.contains("calendar-hidden");

    calendarToggle.textContent = isHidden
      ? "SHOW CALENDAR"
      : "HIDE CALENDAR";

    calendarToggle.setAttribute(
      "aria-expanded",
      String(!isHidden)
    );
  }

  calendarToggle.addEventListener("click", () => {
    calendarWrapper.classList.toggle("calendar-hidden");
    updateCalendarToggle();
  });

  dateSelect.addEventListener("change", async () => {

    if (!dateSelect.value) {
      return;
    }

    selectedDate = new Date(dateSelect.value + "T12:00:00");
    selectedTime = null;

    await updateTimeDropdown();

    selectionMessage.textContent = selectedTime
      ? `${displayDate(selectedDate)} at ${selectedTime}`
      : `No available times on ${displayDate(selectedDate)}.`;

    submitButton.disabled = !selectedTime;

    await renderCalendar();
  });

  timeSelect.addEventListener("change", () => {

    selectedTime = timeSelect.value;

    selectionMessage.textContent =
      `${displayDate(selectedDate)} at ${selectedTime}`;

    submitButton.disabled = !selectedTime;

    renderCalendar();
  });

  previousButton.addEventListener("click", async () => {

    if (currentView === "week") {
      currentDate.setDate(currentDate.getDate() - 7);
    } else {
      currentDate.setMonth(currentDate.getMonth() - 1);
    }

    await renderCalendar();
    await updateDateDropdown();
  });

  nextButton.addEventListener("click", async () => {

    if (currentView === "week") {
      currentDate.setDate(currentDate.getDate() + 7);
    } else {
      currentDate.setMonth(currentDate.getMonth() + 1);
    }

    await renderCalendar();
    await updateDateDropdown();
  });

  document.querySelectorAll(".calendar-view-button")
    .forEach(button => {

      button.addEventListener("click", () => {

        document.querySelectorAll(".calendar-view-button")
          .forEach(button => button.classList.remove("active"));

        button.classList.add("active");

        currentView = button.dataset.view;

        renderCalendar();
      });

    });

  async function initializeSchedule() {

    await renderCalendar();
    await updateDateDropdown();

    // Only true on the reschedule page, where a slot is pre-selected.
    if (selectedDate && selectedTime) {
      await updateTimeDropdown();
      selectionMessage.textContent = `${displayDate(selectedDate)} at ${selectedTime}`;
      selectionMessage.classList.add("has-selection");
      submitButton.disabled = false;
    }

    // Hide calendar by default on mobile.
    if (window.innerWidth <= 700) {
      calendarWrapper.classList.add("calendar-hidden");
    }

    updateCalendarToggle();
  }

  initializeSchedule();

});