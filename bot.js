const { WebcastPushConnection } = require('tiktok-live-connector');

// Data yang ingin dipantau
const HOST_USERNAME = 'username_host';   // Akun yang sedang Live
const TARGET_USERNAME = 'username_target'; // Akun yang dicari di Komal

let tiktokLiveConnection = new WebcastPushConnection(HOST_USERNAME);

tiktokLiveConnection.connect().then(state => {
    console.log(`Berhasil terhubung ke Live milik: ${HOST_USERNAME}`);
}).catch(err => {
    console.log('Host sedang tidak Live atau akun tidak ditemukan.');
});

// Jalankan pengecekan setiap ada pembaharuan penonton/tamu di Komal
tiktokLiveConnection.on('roomUser', data => {
    const dataString = JSON.stringify(data);
    
    // Cek apakah username target ada di dalam payload room host
    if (dataString.includes(TARGET_USERNAME)) {
        console.log(`ALERT! ${TARGET_USERNAME} ditemukan di Komal ${HOST_USERNAME}!`);
        // Di langkah selanjutnya, bagian ini akan memicu notifikasi ke Firebase
    }
});