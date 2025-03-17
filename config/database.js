const mongoose = require('mongoose');

// DigitalOcean MongoDB 클러스터 연결 문자열
const connectionString = 'mongodb+srv://doadmin:5WhxJ7z63820M1qs@db-mongodb-sgp1-31385-27362b68.mongo.ondigitalocean.com/admin?authSource=admin&retryWrites=true&w=majority';

const connectDB = async () => {
    try {
        await mongoose.connect(connectionString);
        
        // viewer 데이터베이스 사용
        mongoose.connection.useDb('viewer');
        
        console.log('MongoDB 클러스터 연결 성공 (viewer 데이터베이스)');
    } catch (error) {
        console.error('MongoDB 연결 오류:', error);
        process.exit(1);
    }
};

module.exports = connectDB; 