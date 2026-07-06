document.getElementById('uploadForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const formData = new FormData(this);
    const resultModal = document.getElementById('resultModal');

    try {
        const response = await fetch('/upload', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (!response.ok) {
            alert(data.error || 'Възникна системна аномалия при обработката.');
            return;
        }

        // Populate display interface with localized variables
        document.getElementById('res_file').innerText = data.filename;
        document.getElementById('res_mat').innerText = data.material_name;
        document.getElementById('res_dim').innerText = `${data.width} x ${data.height}`;
        document.getElementById('res_len').innerText = data.total_length;
        document.getElementById('res_price').innerText = data.price;

        resultModal.classList.remove('hidden');

        // Dynamically insert rows inside saved history log elements
        const libraryBody = document.getElementById('libraryBody');
        const newRow = document.createElement('tr');
        newRow.innerHTML = `
            <td>${data.filename}</td>
            <td>${data.material_name}</td>
            <td>${data.width} x ${data.height}</td>
            <td>${data.total_length}</td>
            <td class="table-price">${data.price} €</td>
        `;
        libraryBody.insertBefore(newRow, libraryBody.firstChild);

    } catch (err) {
        console.error('Transmission Error:', err);
        alert('Връзката със сървъра за ценообразуване беше прекъсната.');
    }
});