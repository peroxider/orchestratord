import { ProjectDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ id: string }> }) { return <ProjectDetailRouteView id={(await params).id} /> }
