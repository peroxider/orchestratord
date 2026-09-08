import { SkillDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ name: string }> }) { const { name } = await params; return <SkillDetailRouteView name={name} /> }
