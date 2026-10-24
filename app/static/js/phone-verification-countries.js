(function () {
  async function loadPhoneCountries() {
    const select = document.getElementById('phoneCountrySelect');
    const status = document.getElementById('phoneCountriesStatus');
    const selectedId = select.value;
    try {
      const response = await fetch('/admin/phone-verification/countries');
      const payload = await response.json();
      if (!response.ok || !payload.success) {
        throw new Error(payload.error || `读取国家列表失败（HTTP ${response.status}）`);
      }
      if (!Array.isArray(payload.countries) || !payload.countries.length) {
        throw new Error('服务端未返回有效的国家列表');
      }
      const options = payload.countries.map((country) => {
        if (typeof country.id !== 'string' || !/^\d+$/.test(country.id)
            || typeof country.name !== 'string' || !country.name.trim()) {
          throw new Error('服务端返回的国家名称或编号无效');
        }
        const option = document.createElement('option');
        option.value = country.id;
        option.textContent = `${country.name}（${country.id}）`;
        return option;
      });
      const currentAvailable = options.some((option) => option.value === selectedId);
      if (!currentAvailable) {
        const savedOption = document.createElement('option');
        savedOption.value = selectedId;
        savedOption.textContent = `已保存国家（${selectedId}，平台未返回）`;
        options.unshift(savedOption);
      }
      select.replaceChildren(...options);
      select.value = selectedId;
      status.textContent = currentAvailable ? '' : `平台未返回已保存国家（${selectedId}），可重新选择国家。`;
      status.hidden = currentAvailable;
      status.classList.toggle('is-error', !currentAvailable);
    } catch (error) {
      status.hidden = false;
      status.classList.add('is-error');
      status.textContent = error.message;
    }
  }

  return loadPhoneCountries();
})();
