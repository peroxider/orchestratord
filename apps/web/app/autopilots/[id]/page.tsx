import { AutopilotDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ id: string }> }) { return <AutopilotDetailRouteView id={(await params).id} /> }
