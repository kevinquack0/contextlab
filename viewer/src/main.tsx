import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import './styles/carbon.scss';
import './styles/app.scss';
import './styles/story.scss';
import './styles/visualizations.scss';

const root = document.getElementById('root');

if (!root) throw new Error('Viewer root element is missing.');

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
