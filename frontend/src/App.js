import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import StudentPortal from './StudentPortal';
import TeacherDashboard from './TeacherDashboard';

function App() {
  return (
    <Router>
      <Routes>
        <Route path="/student" element={<StudentPortal />} />
        <Route path="/teacher" element={<TeacherDashboard />} />
        <Route path="/" element={<div style={{textAlign:'center', marginTop:'100px'}}><h1>SmartSession</h1><p><a href="/student">Student Portal</a> | <a href="/teacher">Teacher Dashboard</a></p></div>} />
      </Routes>
    </Router>
  );
}

export default App;
