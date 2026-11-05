import { createRouter, createWebHistory } from 'vue-router'
// Modified for Xuheng: add an evidence review desk beside the chapter editor.

const Home = () => import('../views/Home.vue')
const Workbench = () => import('../views/Workbench.vue')
const Chapter = () => import('../views/Chapter.vue')
const Cast = () => import('../views/Cast.vue')
const CharacterGraph = () => import('../views/CharacterGraph.vue')
const LocationGraph = () => import('../views/LocationGraph.vue')
const SquareView = () => import('../views/SquareView.vue')
const CharacterSchedulerSimulator = () =>
  import('../components/debug/CharacterSchedulerSimulator.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'Home', component: Home },
    { path: '/about', name: 'About', component: () => import('../views/About.vue') },
    { path: '/book/:slug/workbench', name: 'Workbench', component: Workbench },
    { path: '/book/:slug/cast', name: 'Cast', component: Cast },
    { path: '/book/:slug/operations', name: 'Operations', component: () => import('../views/Operations.vue') },
    { path: '/book/:slug/chapter/:id', name: 'Chapter', component: Chapter },
    { path: '/book/:slug/review/:id', name: 'EvidenceReview', component: () => import('../views/EvidenceReview.vue') },
    { path: '/book/:slug/characters', name: 'CharacterGraph', component: CharacterGraph },
    { path: '/book/:slug/location-graph', name: 'LocationGraph', component: LocationGraph },
    { path: '/square', name: 'Square', component: SquareView },
    {
      path: '/debug/scheduler',
      name: 'CharacterSchedulerSimulator',
      component: CharacterSchedulerSimulator,
    },
  ],
})

export default router
