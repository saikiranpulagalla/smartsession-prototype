import React, { useEffect, useState } from 'react';
import { Line } from 'react-chartjs-2';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js';

/**
 * TEACHER DASHBOARD COMPONENT
 * 
 * This is the real-time monitoring interface for the proctor/teacher.
 * Shows student engagement state, video feed, and historical timeline.
 * 
 * WHAT THE TEACHER SEES:
 * 1. Status badge: "Focused" (green), "Confused" (yellow), "Alert" (red)
 * 2. Live video: Student's camera feed updated every 500ms
 * 3. Timeline chart: Engagement over last ~2.5 minutes with timestamps
 * 4. Alert box: Shows proctoring violations (multiple people, looking away, etc.)
 * 5. Session duration: How long the exam has been running
 * 
 * WHY THESE FEATURES:
 * 
 * - STATUS BADGE:
 *   Teachers need to scan multiple students quickly. Color coding allows 
 *   instant visual identification: red = urgent, yellow = monitor, green = fine.
 *   Text label ("Confused") provides confirmation.
 * 
 * - LIVE VIDEO:
 *   When an alert fires, teacher needs to verify it's real confusion, not
 *   a false positive. Seeing the student's actual face confirms the diagnosis.
 * 
 * - TIMELINE CHART:
 *   Shows engagement trends. E.g., if "Confused" status changes every 3 seconds,
 *   it's probably false positives or the student working through a hard problem.
 *   But if "Confused" lasts 30+ seconds, student probably needs help.
 *   Timestamps help correlate with test content ("Student confused on question 5").
 * 
 * - KEEP-ALIVE PINGS:
 *   Network proxies (office WiFi, corporate firewalls) close idle WebSocket
 *   connections after 5-10 minutes. We send ping every 5 seconds to prevent this.
 * 
 * REAL-TIME ARCHITECTURE:
 * Backend sends updates whenever it receives a new frame from the student.
 * That's ~2 frames/second, so dashboard updates ~2/sec as well.
 * Low latency (WebSocket) means teachers see issues within 1 second.
 */

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend);

const TeacherDashboard = () => {
  // State for student engagement data
  const [studentData, setStudentData] = useState({
    status: 'Connecting...',
    color: 'gray',
    alert: null,
    timeline: [],
    video_frame: null,
    timestamp: null
  });

  // State for UI
  const [connectionStatus, setConnectionStatus] = useState('Connecting to student...');
  const [sessionStartTime, setSessionStartTime] = useState(null);

  // Configuration
  const WEBSOCKET_URL = 'ws://localhost:8000/ws/teacher';
  const KEEPALIVE_INTERVAL_MS = 5000;  // Send ping every 5 seconds

  /**
   * Parse engagement level to human-readable label
   * -1 = Confused (yellow)
   *  0 = Focused/Neutral (green)
   *  1 = Happy/Excited (green)
   */
  const getEngagementLabel = (level) => {
    if (level === -1) return 'Confused';
    if (level === 1) return 'Happy/Excited';
    return 'Focused';
  };

  /**
   * Format timestamp for display on chart X-axis
   * Shows HH:MM:SS format for recent data
   */
  const formatTimeLabel = (index, totalPoints) => {
    if (!studentData.timeline || studentData.timeline.length === 0) return '';
    
    // If we have actual timestamps, use them
    const dataPoint = studentData.timeline[index];
    if (dataPoint && typeof dataPoint[0] === 'number' && dataPoint[0] > 1000000000) {
      const date = new Date(dataPoint[0] * 1000);
      return date.toLocaleTimeString([], { 
        hour: '2-digit', 
        minute: '2-digit',
        second: '2-digit'
      });
    }
    
    // Fallback: show every Nth label to avoid crowding
    const interval = Math.max(1, Math.floor(totalPoints / 10));  // Show ~10 labels
    return (index % interval === 0) ? `${index}` : '';
  };

  /**
   * Convert engagement timeline into Chart.js format for rendering.
   * 
   * VISUAL DESIGN:
   * - Confused (-1): Yellow circle (attracts attention, demands follow-up)
   * - Focused (0): Blue circle (normal state, monitor but not urgent)
   * - Happy (1): Green circle (great, no intervention needed)
   * 
   * The line connecting points shows trend over time. If there are many yellow
   * points in a row, the student is in sustained confusion (needs immediate help).
   * If yellow points are scattered, the student is just working through hard problems.
   * 
   * TECHNICAL NOTES:
   * - Timeline is array of [timestamp, engagement_level] tuples
   * - We convert to [label, value] format for Chart.js
   * - Opacity set to 0.8 so overlapping points show both colors
   */
  const buildChartData = () => {
    if (!studentData.timeline || studentData.timeline.length === 0) {
      return {
        labels: [],
        datasets: [{
          label: 'Student Engagement',
          data: [],
          borderColor: '#2196f3',
          backgroundColor: 'rgba(33, 150, 243, 0.1)',
          tension: 0.3,
          fill: true,
          pointRadius: 2,
        }]
      };
    }

    const labels = studentData.timeline.map((point, index) => 
      formatTimeLabel(index, studentData.timeline.length)
    );

    // Extract engagement levels (second element of [timestamp, level] tuple)
    const engagementLevels = studentData.timeline.map(point => {
      // Handle both array and object formats for backward compatibility
      return Array.isArray(point) ? point[1] : 0;
    });

    // Color each data point based on engagement state
    // Yellow = confused (urgent), Blue = neutral, Green = happy
    const pointColors = engagementLevels.map(level => {
      if (level === -1) return 'rgba(255, 193, 7, 0.8)';  // Confused - yellow/amber
      if (level === 1) return 'rgba(76, 175, 80, 0.8)';   // Happy - green
      return 'rgba(33, 150, 243, 0.8)';                    // Focused - blue
    });

    return {
      labels: labels,
      datasets: [{
        label: 'Student Engagement State',
        data: engagementLevels,
        borderColor: '#2196f3',
        backgroundColor: 'rgba(33, 150, 243, 0.1)',
        pointBackgroundColor: pointColors,  // Each point colored by state
        pointBorderColor: pointColors,
        tension: 0.3,  // Smooth curve, not jagged line
        fill: true,
        pointRadius: 4,
        pointHoverRadius: 6,
      }]
    };
  };

  /**
   * Connect to backend and stream real-time student engagement data.
   * 
   * FLOW:
   * 1. Student sends video frame → /ws/student endpoint
   * 2. Backend processes frame, calculates confusion score
   * 3. Backend broadcasts result to all connected teachers (/ws/teacher)
   * 4. This dashboard receives update and re-renders with new status/chart
   * 
   * KEEP-ALIVE STRATEGY:
   * WebSocket connections are automatically closed by network proxies after
   * ~5-10 minutes of idle time (corporate firewalls, hotel WiFi, etc.).
   * We combat this by sending 'ping' every 5 seconds. This doesn't do anything
   * on the backend, but it keeps the connection "warm" and prevents timeout.
   * 
   * Without keep-alive: Long exam (2+ hours) → connection dies midway → 
   * teacher loses monitoring → bad experience.
   * 
   * CLEANUP:
   * When component unmounts, we clear the interval and close the socket.
   * This prevents memory leaks and ensures new connections when reconnecting.
   */
  useEffect(() => {
    const socket = new WebSocket(WEBSOCKET_URL);

    socket.onopen = () => {
      console.log('Teacher dashboard connected to backend');
      setConnectionStatus('Connected - monitoring student');
      setSessionStartTime(new Date());
    };

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // New data from backend: status, color, timeline, video frame, etc.
        setStudentData(data);
      } catch (err) {
        console.error('Failed to parse backend message:', err);
      }
    };

    socket.onerror = (error) => {
      // Network error, backend unreachable, etc.
      console.error('WebSocket error:', error);
      setConnectionStatus('Connection error');
    };

    socket.onclose = () => {
      // Connection closed (normal or after error)
      console.log('Disconnected from backend');
      setConnectionStatus('Disconnected - attempting to reconnect');
    };

    // KEEP-ALIVE MECHANISM:
    // Send 'ping' message every 5 seconds to prevent proxy timeout
    // This is a no-op on the backend, just keeps connection warm
    const keepAliveInterval = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send('ping');
      }
    }, KEEPALIVE_INTERVAL_MS);

    // CLEANUP: Remove interval and close socket when component unmounts
    return () => {
      clearInterval(keepAliveInterval);
      socket.close();
    };
  }, []);  // Run once on mount

  // Format session duration
  const getSessionDuration = () => {
    if (!sessionStartTime) return '—';
    const elapsed = Math.floor((new Date() - sessionStartTime) / 1000);
    const hours = Math.floor(elapsed / 3600);
    const minutes = Math.floor((elapsed % 3600) / 60);
    const seconds = elapsed % 60;
    
    if (hours > 0) {
      return `${hours}h ${minutes}m ${seconds}s`;
    }
    return `${minutes}m ${seconds}s`;
  };

  const chartData = buildChartData();

  return (
    <div style={{ 
      padding: '20px', 
      maxWidth: '1400px', 
      margin: '0 auto',
      fontFamily: 'Arial, sans-serif',
      backgroundColor: '#f8f9fa',
      minHeight: '100vh'
    }}>
      <h1 style={{ color: '#333', marginBottom: '10px' }}>Teacher Dashboard</h1>
      <p style={{ 
        color: '#666', 
        margin: '0 0 20px 0',
        fontSize: '14px'
      }}>
        Session Duration: {getSessionDuration()} | Backend: {connectionStatus}
      </p>

      {/* Status Panel and Video Feed Grid */}
      <div style={{ 
        display: 'grid', 
        gridTemplateColumns: '1fr 1fr', 
        gap: '20px', 
        marginBottom: '30px'
      }}>
        {/* Status Card */}
        <div style={{
          backgroundColor: studentData.color === 'red' ? '#ffebee' : 
                          studentData.color === 'yellow' ? '#fffde7' : 
                          '#f1f8e9',
          padding: '24px',
          borderRadius: '12px',
          border: `3px solid ${studentData.color === 'red' ? '#ef5350' : 
                               studentData.color === 'yellow' ? '#fbc02d' : 
                               '#7cb342'}`,
          boxShadow: '0 2px 4px rgba(0,0,0,0.1)'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: '16px' }}>
            <div style={{
              width: '20px',
              height: '20px',
              borderRadius: '50%',
              backgroundColor: studentData.color === 'red' ? '#ef5350' : 
                               studentData.color === 'yellow' ? '#fbc02d' : 
                               '#7cb342',
              marginRight: '12px'
            }}></div>
            <h2 style={{ margin: 0, color: '#333' }}>Current Status</h2>
          </div>
          
          <p style={{ 
            fontSize: '28px', 
            fontWeight: 'bold', 
            color: studentData.color === 'red' ? '#c62828' : 
                   studentData.color === 'yellow' ? '#f57f17' : 
                   '#33691e',
            margin: '0 0 12px 0'
          }}>
            {studentData.status}
          </p>

          {studentData.alert && (
            <div style={{
              backgroundColor: '#ffcdd2',
              border: '2px solid #ef5350',
              borderRadius: '8px',
              padding: '12px',
              color: '#c62828',
              fontWeight: 'bold',
              marginTop: '12px'
            }}>
              ⚠️ {studentData.alert}
            </div>
          )}
        </div>

        {/* Video Feed Card */}
        <div style={{
          border: '3px solid #ddd',
          borderRadius: '12px',
          overflow: 'hidden',
          backgroundColor: '#000',
          minHeight: '280px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center'
        }}>
          {studentData.video_frame ? (
            <img 
              src={studentData.video_frame} 
              alt="Student Camera Feed" 
              style={{ 
                width: '100%', 
                height: 'auto', 
                display: 'block',
                maxHeight: '100%'
              }} 
            />
          ) : (
            <div style={{
              color: '#999',
              textAlign: 'center',
              padding: '40px'
            }}>
              <p style={{ fontSize: '16px' }}>Waiting for student video feed...</p>
              <p style={{ fontSize: '12px', color: '#666' }}>
                Ensure student has started their session
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Timeline Chart */}
      <div style={{
        backgroundColor: '#fff',
        padding: '24px',
        borderRadius: '12px',
        boxShadow: '0 2px 8px rgba(0,0,0,0.1)',
        marginBottom: '20px'
      }}>
        <h3 style={{ marginTop: '0', color: '#333' }}>
          Engagement Timeline (Last ~2.5 Minutes)
        </h3>
        
        <div style={{ height: '300px', position: 'relative' }}>
          <Line 
            data={chartData} 
            options={{
              responsive: true,
              maintainAspectRatio: false,
              scales: {
                y: {
                  min: -1.5,
                  max: 1.5,
                  ticks: {
                    callback: function(value) {
                      if (value === -1) return 'Confused';
                      if (value === 0) return 'Focused';
                      if (value === 1) return 'Happy';
                      return '';
                    }
                  },
                  title: {
                    display: true,
                    text: 'Engagement State'
                  }
                },
                x: {
                  title: {
                    display: true,
                    text: 'Time'
                  }
                }
              },
              plugins: {
                legend: {
                  display: true,
                  position: 'top'
                },
                tooltip: {
                  callbacks: {
                    label: function(context) {
                      return 'State: ' + getEngagementLabel(context.parsed.y);
                    }
                  }
                }
              }
            }} 
          />
        </div>

        <div style={{
          marginTop: '16px',
          padding: '12px',
          backgroundColor: '#f5f5f5',
          borderRadius: '8px',
          fontSize: '12px',
          color: '#666'
        }}>
          <strong>Legend:</strong> Green = Focused/Neutral | Yellow = Confused | Blue = Default<br/>
          The chart shows 2 data points per second over the last ~2.5 minutes
        </div>
      </div>

      {/* Quick Stats */}
      <div style={{
        backgroundColor: '#fff',
        padding: '16px',
        borderRadius: '12px',
        boxShadow: '0 2px 8px rgba(0,0,0,0.1)',
        display: 'grid',
        gridTemplateColumns: 'repeat(4, 1fr)',
        gap: '16px'
      }}>
        <div>
          <div style={{ fontSize: '12px', color: '#999', marginBottom: '4px' }}>Data Points</div>
          <div style={{ fontSize: '18px', fontWeight: 'bold', color: '#333' }}>
            {studentData.timeline.length}
          </div>
        </div>
        <div>
          <div style={{ fontSize: '12px', color: '#999', marginBottom: '4px' }}>Current Level</div>
          <div style={{ fontSize: '18px', fontWeight: 'bold', color: '#333' }}>
            {studentData.timeline.length > 0 
              ? getEngagementLabel(studentData.timeline[studentData.timeline.length - 1][1])
              : '—'
            }
          </div>
        </div>
        <div>
          <div style={{ fontSize: '12px', color: '#999', marginBottom: '4px' }}>Status</div>
          <div style={{ fontSize: '18px', fontWeight: 'bold', color: '#333' }}>
            {studentData.status}
          </div>
        </div>
        <div>
          <div style={{ fontSize: '12px', color: '#999', marginBottom: '4px' }}>Last Update</div>
          <div style={{ fontSize: '18px', fontWeight: 'bold', color: '#333' }}>
            {studentData.timestamp 
              ? new Date(studentData.timestamp * 1000).toLocaleTimeString()
              : '—'
            }
          </div>
        </div>
      </div>
    </div>
  );
};

export default TeacherDashboard;

